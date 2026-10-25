from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

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


# ---- fake anthropic SDK ---------------------------------------------------
class _APIError(Exception):
    def __init__(self, message="boom", *, status_code=None, request_id=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id


class _APIStatusError(_APIError): ...
class _BadRequestError(_APIStatusError): ...
class _AuthenticationError(_APIStatusError): ...
class _PermissionDeniedError(_APIStatusError): ...
class _NotFoundError(_APIStatusError): ...
class _RateLimitError(_APIStatusError): ...
class _InternalServerError(_APIStatusError): ...
class _APIConnectionError(_APIError): ...
class _APITimeoutError(_APIConnectionError): ...


FAKE_SDK = SimpleNamespace(
    APIStatusError=_APIStatusError, BadRequestError=_BadRequestError, AuthenticationError=_AuthenticationError,
    PermissionDeniedError=_PermissionDeniedError, NotFoundError=_NotFoundError, RateLimitError=_RateLimitError,
    InternalServerError=_InternalServerError, APIConnectionError=_APIConnectionError, APITimeoutError=_APITimeoutError,
)


def sdk_message(text=GOOD, stop="end_turn", model="test-model-resolved", rid="req_abc", usage=None, content=None, stop_details=None):
    usage = usage or SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=3, cache_creation_input_tokens=None)
    content = content if content is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage, model=model, _request_id=rid, stop_details=stop_details)


class FakeClient:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def img(tmp_path):
    return make_image(tmp_path / "frame.jpg")
