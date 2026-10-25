from __future__ import annotations

import base64
import copy
import inspect
import json
from types import SimpleNamespace

import pytest
from llm_helpers import img, FAKE_SDK, GOOD, SCHEMA, FakeClient, make_request, sdk_message

from trip_vlog.config import parse_config
from trip_vlog.llm import Gateway, make_gateway
from trip_vlog.llm.anthropic_provider import AnthropicProvider
from trip_vlog.llm.schema import sanitize_schema_for_provider
from trip_vlog.llm.types import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMNotFoundError,
    LLMPermissionError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMSchemaError,
    LLMTruncatedError,
)


def gateway(client, **kw):
    return Gateway(AnthropicProvider("test-model", client=client, sdk=FAKE_SDK), **kw)


def test_request_kwargs_exact(img, tmp_path):
    client = FakeClient(sdk_message())
    resp = gateway(client).complete(make_request([img], system="SYSTEM", effort="medium", max_tokens=321))
    assert resp.parsed == {"label": "beach", "score": 7}
    (kw,) = client.calls
    assert set(kw) == {"model", "max_tokens", "system", "messages", "output_config"}  # no temperature/top_p/top_k/thinking
    assert kw["model"] == "test-model" and kw["max_tokens"] == 321 and kw["system"] == "SYSTEM"
    oc = kw["output_config"]
    assert oc["effort"] == "medium" and oc["format"]["type"] == "json_schema"
    assert oc["format"]["schema"] == sanitize_schema_for_provider(SCHEMA)
    (msg,) = kw["messages"]
    assert msg["role"] == "user"
    image, text = msg["content"]
    assert image["type"] == "image" and image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"])[:2] == b"\xff\xd8"
    assert text == {"type": "text", "text": "describe"}


def test_effort_and_system_omitted_when_unset(img):
    client = FakeClient(sdk_message())
    gateway(client).complete(make_request([img], system=""))
    kw = client.calls[0]
    assert "effort" not in kw["output_config"] and "system" not in kw


def test_response_mapping(img):
    resp = gateway(FakeClient(sdk_message(model="test-model-dated", rid="req_xyz"))).complete(make_request([img]))
    assert resp.model == "test-model-dated" and resp.request_id == "req_xyz" and resp.stop_reason == "end_turn"
    u = resp.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_creation_tokens) == (11, 7, 3, 0)


def test_first_text_block_is_used_skipping_other_blocks(img):
    content = [SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=GOOD)]
    assert gateway(FakeClient(sdk_message(content=content))).complete(make_request([img])).parsed["score"] == 7


def test_refusal_and_max_tokens(img):
    details = SimpleNamespace(category="cyber", explanation="not allowed")
    with pytest.raises(LLMRefusalError, match="cyber: not allowed"):
        gateway(FakeClient(sdk_message(text="", stop="refusal", content=[], stop_details=details))).complete(make_request([img]))
    with pytest.raises(LLMTruncatedError):
        gateway(FakeClient(sdk_message(text='{"label": "be', stop="max_tokens"))).complete(make_request([img]))


def test_schema_retry_through_provider(img):
    client = FakeClient(sdk_message(text='{"label": "x", "score": 99}'), sdk_message())
    resp = gateway(client, schema_retries=2).complete(make_request([img]))
    assert resp.attempts == 2
    second = client.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert sum(b["type"] == "image" for m in second for b in m["content"]) == 1
    assert second[1]["content"] == [{"type": "text", "text": '{"label": "x", "score": 99}'}]  # plain assistant turn, not a prefill of a new turn
    assert "$.score" in second[2]["content"][0]["text"]
    with pytest.raises(LLMSchemaError):
        gateway(FakeClient(*[sdk_message(text="{}")] * 3), schema_retries=2).complete(make_request([img]))


@pytest.mark.parametrize("exc, expected", [
    (FAKE_SDK.BadRequestError("bad schema", status_code=400, request_id="r1"), LLMBadRequestError),
    (FAKE_SDK.AuthenticationError("x", status_code=401), LLMAuthError),
    (FAKE_SDK.PermissionDeniedError("x", status_code=403), LLMPermissionError),
    (FAKE_SDK.NotFoundError("x", status_code=404), LLMNotFoundError),
    (FAKE_SDK.RateLimitError("x", status_code=429), LLMRateLimitError),
    (FAKE_SDK.InternalServerError("x", status_code=500), LLMProviderError),
    (FAKE_SDK.APIConnectionError("x"), LLMConnectionError),
    (FAKE_SDK.APITimeoutError("x"), LLMConnectionError),
    (TypeError("Could not resolve authentication method. Expected one of api_key..."), LLMAuthError),
])
def test_error_mapping_and_no_second_retry_loop(img, exc, expected):
    client = FakeClient(exc, sdk_message())
    with pytest.raises(expected) as ei:
        gateway(client).complete(make_request([img]))
    assert type(ei.value) is expected
    assert len(client.calls) == 1  # transport retries belong to the SDK
    if isinstance(exc, FAKE_SDK.BadRequestError):
        assert ei.value.status_code == 400 and ei.value.request_id == "r1" and "bad schema" in str(ei.value)


def test_unknown_exceptions_are_not_swallowed(img):
    with pytest.raises(KeyError):
        gateway(FakeClient(KeyError("x"))).complete(make_request([img]))


def test_missing_model_fails_clearly():
    with pytest.raises(LLMConfigError, match="TRIP_VLOG_LLM_MODEL"):
        AnthropicProvider("", client=FakeClient(), sdk=FAKE_SDK)


# ---- model precedence (config wins, else env) ---------------------------------------------------
def test_model_from_config_wins_over_env(tmp_path, img):
    cfg = parse_config({"media": {"llm_model": "test-model-cfg"}})
    client = FakeClient(sdk_message())
    gw = make_gateway(cfg, tmp_path, "text", env={"TRIP_VLOG_LLM_MODEL": "test-model-env"}, sdk=FAKE_SDK, client=client)
    gw.complete(make_request([img]))
    assert client.calls[0]["model"] == "test-model-cfg" and gw.model == "test-model-cfg"


def test_model_from_env_when_config_unset(tmp_path, img):
    client = FakeClient(sdk_message())
    gw = make_gateway(parse_config({}), tmp_path, "text", env={"TRIP_VLOG_LLM_MODEL": " test-model-env "}, sdk=FAKE_SDK, client=client)
    gw.complete(make_request([img]))
    assert client.calls[0]["model"] == "test-model-env"


def test_no_model_anywhere_is_an_error(tmp_path):
    with pytest.raises(LLMConfigError, match="no LLM model configured"):
        make_gateway(parse_config({}), tmp_path, "text", env={}, sdk=FAKE_SDK, client=FakeClient())


def test_missing_sdk_gives_install_hint(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "anthropic":
            raise ImportError("no anthropic")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LLMConfigError, match=r"pip install -e \"\.\[llm\]\""):
        make_gateway(parse_config({}), tmp_path, "text", env={"TRIP_VLOG_LLM_MODEL": "test-model"})


# ---- schema sanitising ---------------------------------------------------------------------------
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
            "pattern": {"type": "string"},  # a property that is merely NAMED like a keyword must survive
        },
        "required": ["n"],
        "$defs": {"d": {"type": "object", "properties": {"y": {"type": "string"}}}},
    }
    before = copy.deepcopy(schema)
    out = sanitize_schema_for_provider(schema)
    assert schema == before  # input untouched
    assert "$schema" not in out and out["additionalProperties"] is False
    p = out["properties"]
    assert p["n"] == {"type": "integer"}
    assert p["s"] == {"type": "string", "format": "date-time"}
    assert p["odd"] == {"type": "string"}
    assert p["list"]["items"]["additionalProperties"] is False and "minItems" not in p["list"] and "maxItems" not in p["list"]
    assert p["one"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert "pattern" in p
    assert out["$defs"]["d"]["additionalProperties"] is False
    assert sanitize_schema_for_provider({"type": "array", "minItems": 1, "items": {"type": "string"}})["minItems"] == 1


def test_sanitized_schema_is_sent_but_full_schema_validates_client_side(img):
    client = FakeClient(sdk_message(text='{"label": "a", "score": 99}'), sdk_message(text='{"label": "a", "score": 99}'),
                        sdk_message(text='{"label": "a", "score": 99}'))
    with pytest.raises(LLMSchemaError):  # score 99 violates maximum, which the provider never saw
        gateway(client).complete(make_request([img]))
    sent = client.calls[0]["output_config"]["format"]["schema"]
    assert "maximum" not in json.dumps(sent) and sent["additionalProperties"] is False


# ---- real SDK (only when installed) ---------------------------------------------------------------
def test_real_sdk_surface_matches_what_we_send():
    anthropic = pytest.importorskip("anthropic")
    params = inspect.signature(anthropic.resources.messages.Messages.create).parameters
    assert {"model", "max_tokens", "messages", "system", "output_config"} <= set(params)
    from anthropic.types import Message, OutputConfigParam

    assert {"format", "effort"} <= set(OutputConfigParam.__annotations__)
    assert {"stop_reason", "stop_details", "usage", "content", "model"} <= set(Message.model_fields)
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError",
                 "APIStatusError", "APIConnectionError"):
        assert issubclass(getattr(anthropic, name), anthropic.APIError)
    assert issubclass(anthropic.BadRequestError, anthropic.APIStatusError)


def test_real_sdk_exceptions_map(img):
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    req = httpx2.Request("POST", "https://example.invalid/v1/messages")
    resp = httpx2.Response(400, request=req, headers={"request-id": "req_real"})
    exc = anthropic.BadRequestError("schema not supported", response=resp, body={"error": {"type": "invalid_request_error"}})
    client = FakeClient(exc)
    gw = Gateway(AnthropicProvider("test-model", client=client, sdk=anthropic))
    with pytest.raises(LLMBadRequestError) as ei:
        gw.complete(make_request([img]))
    assert ei.value.request_id == "req_real" and ei.value.status_code == 400
