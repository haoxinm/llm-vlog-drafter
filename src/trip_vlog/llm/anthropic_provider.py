"""Anthropic provider, using the official `anthropic` SDK (optional dependency: `pip install -e ".[llm]"`).

Credentials come only from the SDK's own environment resolution (ANTHROPIC_API_KEY, ...). They are never
accepted from project config, never logged, and never stored.
"""

from __future__ import annotations

import base64
from typing import Any

from trip_vlog.llm.schema import sanitize_schema_for_provider
from trip_vlog.llm.types import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMNotFoundError,
    LLMPermissionError,
    LLMProviderError,
    LLMRateLimitError,
    PreparedImage,
    ProviderRequest,
    ProviderResponse,
    TextPart,
    Usage,
)

INSTALL_HINT = 'The "anthropic" package is not installed. Install the optional LLM extra: pip install -e ".[llm]"'


def import_sdk() -> Any:
    try:
        import anthropic
    except ImportError:
        raise LLMConfigError(INSTALL_HINT) from None
    return anthropic


class AnthropicProvider:
    name = "anthropic"
    supports_schema_retry = True

    def __init__(self, model: str, *, client: Any = None, sdk: Any = None, max_retries: int = 2, timeout_seconds: float = 600.0):
        if not model:
            raise LLMConfigError("no model configured: set media.llm_model in project.yaml or the TRIP_VLOG_LLM_MODEL environment variable")
        self.model = model
        self._sdk = sdk if sdk is not None else import_sdk()
        # Transport-level retries (429/5xx/connection) are the SDK's job: max_retries is passed to it, no second loop here.
        self._client = client if client is not None else self._sdk.Anthropic(max_retries=max_retries, timeout=timeout_seconds)

    # -- request --------------------------------------------------------
    def build_kwargs(self, request: ProviderRequest) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": sanitize_schema_for_provider(request.schema)}}
        if request.effort is not None:
            output_config["effort"] = request.effort
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": [_block(p) for p in m.parts]} for m in request.messages],
            "output_config": output_config,
        }
        if request.system:
            kwargs["system"] = request.system
        # Deliberately absent: temperature/top_p/top_k (rejected by current models), `thinking`, assistant prefill.
        return kwargs

    # -- call -----------------------------------------------------------
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        kwargs = self.build_kwargs(request)
        try:
            msg = self._client.messages.create(**kwargs)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001 - mapped below; unknown errors are re-raised untouched
            mapped = self._map_error(e)
            if mapped is None:
                raise
            raise mapped from None
        return self._to_response(msg)

    def _to_response(self, msg: Any) -> ProviderResponse:
        stop = getattr(msg, "stop_reason", None) or "end_turn"
        detail = None
        if stop == "refusal":
            sd = getattr(msg, "stop_details", None)
            if sd is not None:
                parts = [str(x) for x in (getattr(sd, "category", None), getattr(sd, "explanation", None)) if x]
                detail = ": ".join(parts) or None
        text = ""
        for block in getattr(msg, "content", None) or []:  # first text block only (thinking blocks etc. are skipped)
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                break
        u = getattr(msg, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        return ProviderResponse(text=text, stop_reason=str(stop), model=str(getattr(msg, "model", "") or self.model), usage=usage,
                                request_id=getattr(msg, "_request_id", None), stop_detail=detail)

    def _map_error(self, e: Exception) -> LLMError | None:
        sdk = self._sdk
        rid = getattr(e, "request_id", None)
        status = getattr(e, "status_code", None)
        msg = getattr(e, "message", None) or str(e)
        kw = dict(status_code=status, request_id=rid)
        # most specific first
        if isinstance(e, sdk.BadRequestError):
            return LLMBadRequestError(f"Anthropic rejected the request (400): {msg}", **kw)
        if isinstance(e, sdk.AuthenticationError):
            return LLMAuthError("Anthropic authentication failed (401): check ANTHROPIC_API_KEY or your other SDK credentials", **kw)
        if isinstance(e, sdk.PermissionDeniedError):
            return LLMPermissionError(f"Anthropic denied permission (403): {msg}", **kw)
        if isinstance(e, sdk.NotFoundError):
            return LLMNotFoundError(f"Anthropic returned 404 (is the configured model id valid for your account?): {msg}", **kw)
        if isinstance(e, sdk.RateLimitError):
            return LLMRateLimitError(f"Anthropic rate limit (429) persisted after the SDK's retries: {msg}", **kw)
        if isinstance(e, sdk.APIStatusError):
            return LLMProviderError(f"Anthropic API error ({status}): {msg}", **kw)
        if isinstance(e, sdk.APIConnectionError):
            return LLMConnectionError(f"cannot reach the Anthropic API after the SDK's retries: {msg}", **kw)
        if isinstance(e, TypeError) and "authentication method" in str(e):  # the SDK found no credentials at all
            return LLMAuthError("no Anthropic credentials found: set the ANTHROPIC_API_KEY environment variable (see README, \"LLM provider\")")
        return None


def _block(part: TextPart | PreparedImage) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    return {"type": "image", "source": {"type": "base64", "media_type": part.media_type,
                                        "data": base64.standard_b64encode(part.data).decode("ascii")}}
