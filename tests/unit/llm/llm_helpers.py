from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from trip_vlog.config import parse_config
from trip_vlog.llm.http_provider import HttpProvider
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.llm.types import ImagePart, LLMRequest, ProviderRequest, ProviderResponse, TextPart, Usage

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "minLength": 1},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
    },
    "required": ["label", "score"],
    "additionalProperties": False,
}
GOOD = '{"label": "beach", "score": 7}'
BAD = '{"label": "beach", "score": 99}'


def make_image(path: Path, size=(64, 48), color=(200, 30, 30)) -> Path:
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def make_request(images=(), *, text="describe", version="1", system="sys", effort=None, max_tokens=500, schema=None) -> LLMRequest:
    parts = [ImagePart(p) for p in images] + [TextPart(text)]
    return LLMRequest("media_analyst", version, system, tuple(parts), schema or SCHEMA, max_tokens, effort)


def presp(text=GOOD, stop="end_turn", model="test-model-resolved", usage=Usage(10, 5, 1, 2), rid="req_1", detail=None) -> ProviderResponse:
    return ProviderResponse(text=text, stop_reason=stop, model=model, usage=usage, request_id=rid, stop_detail=detail)


class ScriptedProvider:
    """Provider double: returns queued ProviderResponses (or raises queued exceptions) and records requests."""

    name = "scripted"

    def __init__(self, *responses, model="test-model", supports_schema_retry=True):
        self.model = model
        self.supports_schema_retry = supports_schema_retry
        self.queue = list(responses)
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ---- fake HTTP transport -----------------------------------------------------------------------------
class FakeTransport:
    """Records HttpRequests; returns queued HttpResponses (or raises queued exceptions). No network."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last_body(self):
        return json.loads(self.requests[-1].body.decode("utf-8"))


def http_json(obj, status=200, headers=None) -> HttpResponse:
    return HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, json.dumps(obj).encode("utf-8"))


def anthropic_reply(text=GOOD, stop="end_turn", model="test-model-resolved", **extra):
    return http_json({"id": "msg_1", "type": "message", "model": model, "stop_reason": stop,
                      "content": [{"type": "text", "text": text}] if text is not None else [],
                      "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2},
                      **extra}, headers={"request-id": "req_hdr"})


def chat_reply(text=GOOD, finish="stop", model="test-model-resolved", refusal=None):
    msg = {"role": "assistant", "content": text}
    if refusal:
        msg["refusal"] = refusal
    return http_json({"id": "chatcmpl-1", "model": model, "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                      "usage": {"prompt_tokens": 11, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 3}}})


def responses_reply(text=GOOD, status="completed", incomplete=None, refusal=None, model="test-model-resolved"):
    content = [{"type": "refusal", "refusal": refusal}] if refusal else [{"type": "output_text", "text": text}]
    d = {"id": "resp_1", "model": model, "status": status, "output": [{"type": "reasoning", "summary": []},
         {"type": "message", "role": "assistant", "content": content}],
         "usage": {"input_tokens": 11, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 3}}}
    if incomplete:
        d["incomplete_details"] = {"reason": incomplete}
    return http_json(d)


def endpoint_config(api="anthropic_messages", local=False, **ep):
    base = {"api": api, "base_url": "http://127.0.0.1:9999" if local else {
        "anthropic_messages": "https://api.anthropic.com"}.get(api, "https://api.openai.com"), "model": "test-model"}
    return parse_config({"llm": {"endpoints": {"main": {**base, **ep}}}})


def provider_for(cfg, transport, env=None, sleep=None, profile="main"):
    return HttpProvider(profile, cfg.llm.endpoints[profile], model="test-model", env=env if env is not None else {"ANTHROPIC_API_KEY": "sk-ant-test-key-1",
                        "OPENAI_API_KEY": "sk-openai-test-key-2"}, transport=transport, sleep=sleep or (lambda s: None))


@pytest.fixture
def img(tmp_path):
    return make_image(tmp_path / "frame.jpg")
