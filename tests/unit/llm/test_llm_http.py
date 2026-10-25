from __future__ import annotations

import base64
import copy
import json

import pytest
from llm_helpers import (
    BAD,
    GOOD,
    SCHEMA,
    FakeTransport,
    anthropic_reply,
    chat_reply,
    endpoint_config,
    http_json,
    img,  # noqa: F401
    make_request,
    provider_for,
    responses_reply,
)

from trip_vlog.config import parse_config
from trip_vlog.llm import Gateway, make_gateway
from trip_vlog.llm.schema import sanitize_schema_for_provider
from trip_vlog.llm.transport import HttpResponse, TransportConnectionError, TransportTimeout
from trip_vlog.llm.types import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMInputError,
    LLMNotFoundError,
    LLMPermissionError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTruncatedError,
)
from trip_vlog.logging_utils import RunLogger


def gw(cfg, transport, **kw):
    return Gateway(provider_for(cfg, transport, **kw))


def b64_of(img):
    from trip_vlog.llm.images import prepare_image
    from trip_vlog.llm.types import ImagePart

    return base64.b64encode(prepare_image(ImagePart(img)).data).decode()


# ---------------------------------------------------------------- anthropic_messages
def test_anthropic_exact_request(img):
    t = FakeTransport(anthropic_reply())
    cfg = endpoint_config("anthropic_messages")
    resp = gw(cfg, t).complete(make_request([img], system="SYSTEM", effort="medium", max_tokens=321))
    (r,) = t.requests
    assert r.method == "POST" and r.url == "https://api.anthropic.com/v1/messages" and r.timeout == 120.0
    assert r.headers["x-api-key"] == "sk-ant-test-key-1" and r.headers["anthropic-version"] == "2023-06-01"
    assert r.headers["content-type"] == "application/json" and "authorization" not in r.headers
    body = t.last_body
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}  # no temperature/thinking unless params say so
    assert body["model"] == "test-model" and body["max_tokens"] == 321 and body["system"] == "SYSTEM"
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": sanitize_schema_for_provider(SCHEMA)}, "effort": "medium"}
    (msg,) = body["messages"]
    assert msg["role"] == "user" and msg["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_of(img)}},
        {"type": "text", "text": "describe"}]
    assert resp.parsed == {"label": "beach", "score": 7} and resp.model == "test-model-resolved"
    assert resp.request_id == "req_hdr" and resp.stop_reason == "end_turn"
    u = resp.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens) == (11, 7, 3, 2)


def test_anthropic_sanitized_schema_sent_full_schema_validated_locally(img):
    t = FakeTransport(*[anthropic_reply('{"label": "a", "score": 99}')] * 3)
    with pytest.raises(LLMSchemaError):  # score 99 violates `maximum`, which the server never saw
        gw(endpoint_config(), t).complete(make_request([img]))
    sent = t.last_body["output_config"]["format"]["schema"]
    assert "maximum" not in json.dumps(sent) and sent["additionalProperties"] is False


def test_anthropic_refusal_and_truncation(img):
    stop_details = {"type": "refusal", "category": "cyber", "explanation": "not allowed"}
    with pytest.raises(LLMRefusalError, match="cyber: not allowed"):
        gw(endpoint_config(), FakeTransport(anthropic_reply(None, stop="refusal", stop_details=stop_details))).complete(make_request([img]))
    with pytest.raises(LLMTruncatedError):
        gw(endpoint_config(), FakeTransport(anthropic_reply('{"label": "be', stop="max_tokens"))).complete(make_request([img]))


def test_anthropic_skips_non_text_blocks(img):
    reply = http_json({"model": "m", "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
                       "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": GOOD}]})
    assert gw(endpoint_config(), FakeTransport(reply)).complete(make_request([img])).parsed["score"] == 7


# ---------------------------------------------------------------- openai_chat
def test_openai_chat_exact_request(img):
    t = FakeTransport(chat_reply())
    cfg = endpoint_config("openai_chat")
    resp = gw(cfg, t).complete(make_request([img], system="SYSTEM", effort="low", max_tokens=300))
    (r,) = t.requests
    assert r.url == "https://api.openai.com/v1/chat/completions"
    assert r.headers["authorization"] == "Bearer sk-openai-test-key-2" and "x-api-key" not in r.headers
    body = t.last_body
    assert set(body) == {"model", "messages", "max_tokens", "response_format", "reasoning_effort"}
    assert body["max_tokens"] == 300 and body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "media_analyst", "schema": sanitize_schema_for_provider(SCHEMA), "strict": True}}
    sys_msg, user = body["messages"]
    assert sys_msg == {"role": "system", "content": "SYSTEM"}
    assert user == {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64_of(img)}},
        {"type": "text", "text": "describe"}]}
    assert resp.model == "test-model-resolved" and resp.request_id == "chatcmpl-1"
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_tokens) == (11, 7, 3)


def test_openai_chat_text_only_message_is_a_plain_string_and_retry_turns(img):
    t = FakeTransport(chat_reply(BAD), chat_reply(GOOD))
    resp = gw(endpoint_config("openai_chat"), t).complete(make_request([], text="hello"))
    assert resp.attempts == 2
    msgs = t.last_body["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "hello" and msgs[2]["content"] == BAD and "$.score" in msgs[3]["content"]


def test_openai_chat_token_field_override_and_params(img):
    cfg = endpoint_config("openai_chat", max_tokens_field="max_completion_tokens", params={"temperature": 0.2, "seed": 7, "reasoning_effort": "high"}, max_tokens=100)
    t = FakeTransport(chat_reply())
    gw(cfg, t).complete(make_request([img], max_tokens=500))
    body = t.last_body
    assert body["max_completion_tokens"] == 100 and "max_tokens" not in body  # also capped by the profile
    assert body["temperature"] == 0.2 and body["seed"] == 7 and body["reasoning_effort"] == "high"


def test_openai_chat_refusal_truncation_content_filter(img):
    with pytest.raises(LLMRefusalError, match="cannot help"):
        gw(endpoint_config("openai_chat"), FakeTransport(chat_reply(None, refusal="cannot help"))).complete(make_request([img]))
    with pytest.raises(LLMRefusalError):
        gw(endpoint_config("openai_chat"), FakeTransport(chat_reply("", finish="content_filter"))).complete(make_request([img]))
    with pytest.raises(LLMTruncatedError):
        gw(endpoint_config("openai_chat"), FakeTransport(chat_reply('{"la', finish="length"))).complete(make_request([img]))
    with pytest.raises(LLMProviderError, match="tool_calls"):
        gw(endpoint_config("openai_chat"), FakeTransport(chat_reply("", finish="tool_calls"))).complete(make_request([img]))


# ---------------------------------------------------------------- openai_responses
def test_openai_responses_exact_request(img):
    t = FakeTransport(responses_reply())
    resp = gw(endpoint_config("openai_responses"), t).complete(make_request([img], system="SYSTEM", effort="high", max_tokens=250))
    (r,) = t.requests
    assert r.url == "https://api.openai.com/v1/responses" and r.headers["authorization"] == "Bearer sk-openai-test-key-2"
    body = t.last_body
    assert set(body) == {"model", "instructions", "input", "max_output_tokens", "text", "reasoning"}
    assert body["instructions"] == "SYSTEM" and body["max_output_tokens"] == 250 and body["reasoning"] == {"effort": "high"}
    assert body["text"] == {"format": {"type": "json_schema", "name": "media_analyst", "schema": sanitize_schema_for_provider(SCHEMA), "strict": True}}
    assert body["input"] == [{"role": "user", "content": [
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_of(img)},
        {"type": "input_text", "text": "describe"}]}]
    assert resp.parsed["label"] == "beach" and resp.request_id == "resp_1" and resp.model == "test-model-resolved"
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_tokens) == (11, 7, 3)


def test_openai_responses_retry_turns_use_output_text_for_assistant(img):
    t = FakeTransport(responses_reply(BAD), responses_reply(GOOD))
    gw(endpoint_config("openai_responses"), t).complete(make_request([img]))
    inp = t.last_body["input"]
    assert inp[1] == {"role": "assistant", "content": [{"type": "output_text", "text": BAD}]}
    assert inp[2]["content"][0]["type"] == "input_text"


def test_openai_responses_refusal_truncation_failure(img):
    ep = endpoint_config("openai_responses")
    with pytest.raises(LLMRefusalError, match="no thanks"):
        gw(ep, FakeTransport(responses_reply(refusal="no thanks"))).complete(make_request([img]))
    with pytest.raises(LLMTruncatedError):
        gw(ep, FakeTransport(responses_reply('{"a', status="incomplete", incomplete="max_output_tokens"))).complete(make_request([img]))
    with pytest.raises(LLMRefusalError):
        gw(ep, FakeTransport(responses_reply("", status="incomplete", incomplete="content_filter"))).complete(make_request([img]))
    bad = http_json({"status": "failed", "error": {"message": "server exploded"}, "output": []})
    with pytest.raises(LLMProviderError, match="server exploded"):
        gw(ep, FakeTransport(bad)).complete(make_request([img]))


# ---------------------------------------------------------------- path / structured output modes
@pytest.mark.parametrize("path,expect", [(None, "/v1/chat/completions"), ("/openai/v1/chat/completions", "/openai/v1/chat/completions"),
                                         ("chat", "/chat")])
def test_path_override(img, path, expect):
    t = FakeTransport(chat_reply())
    gw(endpoint_config("openai_chat", base_url="https://host.example/", path=path), t).complete(make_request([img]))
    assert t.requests[0].url == "https://host.example" + expect


@pytest.mark.parametrize("api,reply", [("anthropic_messages", anthropic_reply), ("openai_chat", chat_reply), ("openai_responses", responses_reply)])
def test_json_prompt_mode_has_no_native_schema_but_prompts_for_json(img, api, reply):
    t = FakeTransport(reply("Sure! Here you go:\n```json\n" + GOOD + "\n```\nHope that helps."))
    resp = gw(endpoint_config(api, structured_output="json_prompt"), t).complete(make_request([img], system="SYSTEM"))
    body = t.last_body
    blob = json.dumps(body)
    assert "response_format" not in body and "output_config" not in body and "text" not in body
    sys_text = body.get("system") or body.get("instructions") or body["messages"][0]["content"]
    assert sys_text.startswith("SYSTEM") and "ONLY one JSON value" in sys_text and '"additionalProperties":false' in sys_text
    assert '"maximum":10' in sys_text  # the full schema is shown to the model
    assert resp.parsed == {"label": "beach", "score": 7} and "json_schema" not in blob


@pytest.mark.parametrize("api,reply", [("anthropic_messages", anthropic_reply), ("openai_chat", chat_reply), ("openai_responses", responses_reply)])
def test_none_mode_sends_no_schema_at_all(img, api, reply):
    t = FakeTransport(reply(GOOD))
    gw(endpoint_config(api, structured_output="none"), t).complete(make_request([img], system="SYSTEM"))
    assert "json_schema" not in json.dumps(t.last_body) and "ONLY one JSON" not in json.dumps(t.last_body)


def test_json_extraction_from_prose_and_failure_retry(img):
    t = FakeTransport(chat_reply("The answer is {\"label\": \"beach\", \"score\": 3} - done"))
    assert gw(endpoint_config("openai_chat", structured_output="json_prompt"), t).complete(make_request([img])).parsed["score"] == 3
    t = FakeTransport(chat_reply("no json here"), chat_reply(GOOD))
    resp = gw(endpoint_config("openai_chat", structured_output="json_prompt"), t).complete(make_request([img]))
    assert resp.attempts == 2


def test_supports_images_false_rejects_image_requests(img):
    t = FakeTransport(chat_reply())
    with pytest.raises(LLMInputError, match="supports_images"):
        gw(endpoint_config("openai_chat", supports_images=False), t).complete(make_request([img]))
    assert not t.requests
    gw(endpoint_config("openai_chat", supports_images=False), FakeTransport(chat_reply())).complete(make_request([], text="text only"))


# ---------------------------------------------------------------- transport retries / errors
def test_retry_on_429_and_503_honours_retry_after(img):
    sleeps = []
    t = FakeTransport(http_json({"error": {"message": "slow down"}}, 429, {"Retry-After": "7"}),
                      http_json({"error": {"message": "busy"}}, 503), chat_reply())
    resp = gw(endpoint_config("openai_chat"), t, sleep=sleeps.append).complete(make_request([img]))
    assert resp.parsed["label"] == "beach" and len(t.requests) == 3
    assert sleeps == [7.0, 1.0]  # Retry-After first, then exponential backoff (0.5 * 2**1)
    assert resp.attempts == 1  # attempts counts model calls, not transport retries


def test_retry_after_is_capped_and_http_date_ignored(img):
    sleeps = []
    t = FakeTransport(http_json({}, 429, {"Retry-After": "9999"}), http_json({}, 429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), chat_reply())
    gw(endpoint_config("openai_chat"), t, sleep=sleeps.append).complete(make_request([img]))
    assert sleeps == [60.0, 1.0]


def test_retries_exhausted_maps_to_typed_error(img):
    sleeps = []
    t = FakeTransport(*[http_json({"error": {"message": "nope"}}, 429)] * 3)
    with pytest.raises(LLMRateLimitError, match="nope"):
        gw(endpoint_config("openai_chat", max_retries=2), t, sleep=sleeps.append).complete(make_request([img]))
    assert len(t.requests) == 3 and len(sleeps) == 2
    t = FakeTransport(http_json({}, 503))
    with pytest.raises(LLMProviderError) as ei:
        gw(endpoint_config("openai_chat", max_retries=0), t).complete(make_request([img]))
    assert ei.value.status_code == 503 and type(ei.value) is LLMProviderError and len(t.requests) == 1


def test_connection_errors_retry_then_raise(img):
    sleeps = []
    t = FakeTransport(TransportConnectionError("ConnectionRefusedError"), chat_reply())
    gw(endpoint_config("openai_chat"), t, sleep=sleeps.append).complete(make_request([img]))
    assert sleeps == [0.5]
    t = FakeTransport(*[TransportConnectionError("refused")] * 3)
    with pytest.raises(LLMConnectionError, match="3 attempt"):
        gw(endpoint_config("openai_chat"), t, sleep=lambda s: None).complete(make_request([img]))


def test_timeout_is_typed_and_not_retried(img):
    t = FakeTransport(TransportTimeout("t"), chat_reply())
    with pytest.raises(LLMTimeoutError, match="timeout_seconds"):
        gw(endpoint_config("openai_chat", timeout_seconds=5), t).complete(make_request([img]))
    assert len(t.requests) == 1 and t.requests[0].timeout == 5


@pytest.mark.parametrize("status,expected", [(400, LLMBadRequestError), (422, LLMBadRequestError), (401, LLMAuthError),
                                             (403, LLMPermissionError), (404, LLMNotFoundError), (418, LLMProviderError)])
def test_status_mapping_without_retry(img, status, expected):
    t = FakeTransport(http_json({"error": {"message": "why", "type": "x"}}, status, {"x-request-id": "rid"}), chat_reply())
    with pytest.raises(expected) as ei:
        gw(endpoint_config("openai_chat"), t).complete(make_request([img]))
    assert type(ei.value) is expected and len(t.requests) == 1
    assert ei.value.status_code == status and ei.value.request_id == "rid"


def test_non_json_and_wrong_shape_replies(img):
    with pytest.raises(LLMProviderError, match="non-JSON"):
        gw(endpoint_config("openai_chat"), FakeTransport(HttpResponse(200, {}, b"<html>proxy</html>"))).complete(make_request([img]))
    with pytest.raises(LLMProviderError, match="api"):
        gw(endpoint_config("openai_chat"), FakeTransport(anthropic_reply())).complete(make_request([img]))
    with pytest.raises(LLMProviderError):
        gw(endpoint_config("openai_chat"), FakeTransport(http_json([1, 2]))).complete(make_request([img]))


# ---------------------------------------------------------------- credentials
def test_key_from_env_never_logged_or_leaked_in_errors(img, tmp_path):
    key = "sk-live-VERYSECRET-123456"
    logger = RunLogger(tmp_path / "run.jsonl", tmp_path / "err.jsonl", "r1")
    t = FakeTransport(http_json({"error": {"message": f"bad key {key} supplied"}}, 401), chat_reply())
    g = Gateway(provider_for(endpoint_config("openai_chat"), t, env={"OPENAI_API_KEY": key}), logger=logger)
    with pytest.raises(LLMAuthError) as ei:
        g.complete(make_request([img]))
    assert key not in str(ei.value)
    Gateway(provider_for(endpoint_config("openai_chat"), FakeTransport(chat_reply()), env={"OPENAI_API_KEY": key}), logger=logger).complete(make_request([img]))
    assert t.requests[0].headers["authorization"] == f"Bearer {key}"
    logs = (tmp_path / "run.jsonl").read_text() + (tmp_path / "err.jsonl").read_text()
    assert "VERYSECRET" not in logs and "/9j/" not in logs


def test_missing_key_env_is_a_clear_config_error_and_null_means_no_auth(img):
    cfg = endpoint_config("openai_chat")
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        provider_for(cfg, FakeTransport(), env={})
    t = FakeTransport(chat_reply())
    cfg = endpoint_config("openai_chat", local=True, api_key_env=None)
    gw(cfg, t, env={}).complete(make_request([img]))
    assert "authorization" not in t.requests[0].headers and t.requests[0].url == "http://127.0.0.1:9999/v1/chat/completions"


def test_custom_api_key_env_and_header_expansion(img):
    cfg = endpoint_config("openai_chat", api_key_env="MY_HOST_KEY", headers={"X-Org": "org-${ORG_ID}", "HTTP-Referer": "https://x"})
    t = FakeTransport(chat_reply())
    gw(cfg, t, env={"MY_HOST_KEY": "k-123456", "ORG_ID": "42"}).complete(make_request([img]))
    h = t.requests[0].headers
    assert h["authorization"] == "Bearer k-123456" and h["x-org"] == "org-42" and h["http-referer"] == "https://x"
    with pytest.raises(LLMConfigError, match="ORG_ID"):
        provider_for(cfg, FakeTransport(), env={"MY_HOST_KEY": "k-123456"})


# ---------------------------------------------------------------- model precedence / factory
def mk(tmp_path, raw, env, t, **kw):
    return make_gateway(parse_config(raw), tmp_path, "text", env=env, transport=t, **kw)


def test_model_config_wins_over_env_then_env_then_error(tmp_path, img):
    raw = {"llm": {"endpoints": {"main": {"model": "test-model-cfg"}}}}
    env = {"TRIP_VLOG_LLM_MODEL": "test-model-env", "ANTHROPIC_API_KEY": "k-123456"}
    t = FakeTransport(anthropic_reply())
    g = mk(tmp_path, raw, env, t)
    g.complete(make_request([img]))
    assert t.last_body["model"] == "test-model-cfg" and g.model == "test-model-cfg"
    t = FakeTransport(anthropic_reply())
    mk(tmp_path, {}, env, t).complete(make_request([img]))
    assert t.last_body["model"] == "test-model-env"
    custom = {"llm": {"endpoints": {"main": {"model_env": "MY_MODEL"}}}}
    t = FakeTransport(anthropic_reply())
    mk(tmp_path, custom, {**env, "MY_MODEL": "test-model-x"}, t).complete(make_request([img]))
    assert t.last_body["model"] == "test-model-x"
    with pytest.raises(LLMConfigError, match="no model configured"):
        mk(tmp_path, {}, {"ANTHROPIC_API_KEY": "k-123456"}, FakeTransport())


def test_text_and_vision_use_different_profiles(tmp_path, img):
    raw = {"llm": {"text": "cloud", "vision": "local", "endpoints": {
        "cloud": {"api": "openai_responses", "model": "test-model"},
        "local": {"api": "openai_chat", "base_url": "http://localhost:11434", "api_key_env": None, "model": "test-model-local",
                  "structured_output": "json_prompt"}}}}
    cfg = parse_config(raw)
    env = {"OPENAI_API_KEY": "k-123456"}
    tv, tt = FakeTransport(chat_reply()), FakeTransport(responses_reply())
    gv = make_gateway(cfg, tmp_path, "vision", env=env, transport=tv)
    gt = make_gateway(cfg, tmp_path, "text", env=env, transport=tt)
    gv.complete(make_request([img]))
    gt.complete(make_request([], text="plan"))
    assert tv.requests[0].url == "http://localhost:11434/v1/chat/completions" and tt.requests[0].url == "https://api.openai.com/v1/responses"
    assert gv.provider.is_local and not gt.provider.is_local


def test_local_vs_remote_is_logged_per_call(tmp_path, img):
    logger = RunLogger(tmp_path / "run.jsonl", tmp_path / "e.jsonl", "r")
    raw = {"llm": {"endpoints": {"main": {"api": "openai_chat", "base_url": "http://127.0.0.1:8080", "api_key_env": None, "model": "test-model"}}}}
    mk(tmp_path, raw, {}, FakeTransport(chat_reply()), logger=logger).complete(make_request([img]))
    mk(tmp_path, {"llm": {"endpoints": {"main": {"model": "test-model"}}}}, {"ANTHROPIC_API_KEY": "k-123456"},
       FakeTransport(anthropic_reply()), logger=logger).complete(make_request([img], text="other"))
    from trip_vlog.logging_utils import read_jsonl

    rows = read_jsonl(tmp_path / "run.jsonl")
    assert [(r["endpoint"], r["local"]) for r in rows] == [("main", True), ("main", False)]
    assert rows[0]["provider"] == "http:openai_chat"


# ---------------------------------------------------------------- schema sanitising
def test_sanitize_schema():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 0, "maximum": 5, "multipleOf": 1},
            "s": {"type": "string", "minLength": 1, "maxLength": 9, "pattern": "^a", "format": "date-time"},
            "odd": {"type": "string", "format": "weird"},
            "list": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "object", "properties": {"x": {"type": "number"}}, "additionalProperties": True}},
            "one": {"oneOf": [{"type": "string"}, {"type": "null"}]},
            "pattern": {"type": "string"},  # a property merely NAMED like a keyword must survive
        },
        "required": ["n"],
        "$defs": {"d": {"type": "object", "properties": {"y": {"type": "string"}}}},
    }
    before = copy.deepcopy(schema)
    out = sanitize_schema_for_provider(schema)
    assert schema == before
    assert "$schema" not in out and out["additionalProperties"] is False
    p = out["properties"]
    assert p["n"] == {"type": "integer"}
    assert p["s"] == {"type": "string", "format": "date-time"} and p["odd"] == {"type": "string"}
    assert p["list"]["items"]["additionalProperties"] is False and "minItems" not in p["list"] and "maxItems" not in p["list"]
    assert p["one"] == {"anyOf": [{"type": "string"}, {"type": "null"}]} and "pattern" in p
    assert out["$defs"]["d"]["additionalProperties"] is False
    assert sanitize_schema_for_provider({"type": "array", "minItems": 1, "items": {"type": "string"}})["minItems"] == 1
