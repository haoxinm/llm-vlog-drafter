"""Wire-format adapters for the three supported HTTP API families.

IMPORTANT: the request/response shapes below follow the providers' public API documentation as understood when this was
written. They could NOT be exercised against live servers in development (tests use a fake transport), so verify them
against your server, and use `llm.endpoints.<name>.path`, `params`, `max_tokens_field` and `structured_output` to adapt.

  anthropic_messages  POST {base}/v1/messages           x-api-key, anthropic-version; output_config.format json_schema
  openai_chat         POST {base}/v1/chat/completions   Bearer; response_format json_schema (strict)
  openai_responses    POST {base}/v1/responses          Bearer; text.format json_schema (strict)

Each adapter maps the API's stop/finish states onto the gateway's common stop reasons:
  end_turn   -> complete
  max_tokens -> truncated (never used, never silently cut)
  refusal    -> refused
  anything else is passed through as-is and the gateway raises a provider error for it.

Strict structured output on OpenAI additionally requires every property to be listed in `required`; schemas meant for
those endpoints should do so (the gateway still validates the full schema client-side in every mode).
"""

from __future__ import annotations

import base64
import re
from typing import Any, Mapping

from trip_vlog.config import EndpointConfig
from trip_vlog.llm.schema import sanitize_schema_for_provider
from trip_vlog.llm.types import (
    LLMProviderError,
    PreparedImage,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    TextPart,
    Usage,
)

ANTHROPIC_VERSION = "2023-06-01"


def _data_url(img: PreparedImage) -> str:
    return f"data:{img.media_type};base64,{base64.standard_b64encode(img.data).decode('ascii')}"


def _schema_name(req: ProviderRequest) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", req.template_id)[:64] or "response"


def _int(d: Any, *keys: str) -> int:
    for k in keys:
        if isinstance(d, Mapping) and isinstance(d.get(k), (int, float)):
            return int(d[k])
    return 0


def _usage(d: Mapping[str, Any]) -> Mapping[str, Any]:
    u = d.get("usage")
    return u if isinstance(u, Mapping) else {}


def _msg(data: Any, fallback: str = "malformed reply") -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise LLMProviderError(f"unexpected reply from the server (is `api` correct for this endpoint?): {fallback}")
    return data


def error_message(data: Any, raw: bytes) -> str:
    """Best-effort human text from an error body ({"error": {"message": ...}} / {"error": "..."} / raw)."""
    if isinstance(data, Mapping):
        err = data.get("error")
        if isinstance(err, Mapping) and isinstance(err.get("message"), str):
            return err["message"][:500]
        if isinstance(err, str):
            return err[:500]
        if isinstance(data.get("message"), str):
            return data["message"][:500]
    return raw[:300].decode("utf-8", "replace").strip()


OPENAI_HOST = "api.openai.com"


class Adapter:
    api: str
    default_path: str
    default_token_field: str

    def token_field(self, ep: EndpointConfig) -> str:
        """Body field that carries the output-token limit: the profile's `max_tokens_field`, else the adapter's default."""
        return ep.max_tokens_field or self.default_token_field

    def headers(self, api_key: str | None) -> dict[str, str]:
        h = {"content-type": "application/json", "accept": "application/json"}
        if api_key:
            h.update(self._auth(api_key))
        return h

    def _auth(self, api_key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {api_key}"}

    def build(self, req: ProviderRequest, ep: EndpointConfig, *, system: str, native: bool, max_tokens: int) -> dict[str, Any]:
        raise NotImplementedError

    def parse(self, data: Any, headers: Mapping[str, str]) -> ProviderResponse:
        raise NotImplementedError

    @staticmethod
    def _request_id(data: Mapping[str, Any], headers: Mapping[str, str]) -> str | None:
        return headers.get("request-id") or headers.get("x-request-id") or (data.get("id") if isinstance(data.get("id"), str) else None)


# ------------------------------------------------------------------ anthropic_messages
class AnthropicMessages(Adapter):
    api = "anthropic_messages"
    default_path = "/v1/messages"
    default_token_field = "max_tokens"

    def _auth(self, api_key: str) -> dict[str, str]:
        return {"x-api-key": api_key}

    def headers(self, api_key: str | None) -> dict[str, str]:
        h = super().headers(api_key)
        h["anthropic-version"] = ANTHROPIC_VERSION
        return h

    @staticmethod
    def _block(p: TextPart | PreparedImage) -> dict[str, Any]:
        if isinstance(p, TextPart):
            return {"type": "text", "text": p.text}
        return {"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": base64.standard_b64encode(p.data).decode("ascii")}}

    def build(self, req, ep, *, system, native, max_tokens):
        body: dict[str, Any] = dict(ep.params)
        output_config = dict(body.pop("output_config", None) or {})
        if native:
            output_config["format"] = {"type": "json_schema", "schema": sanitize_schema_for_provider(req.schema)}
        if req.effort is not None:
            output_config["effort"] = req.effort
        body.update(
            model=req.model,
            messages=[{"role": m.role, "content": [self._block(p) for p in m.parts]} for m in req.messages],
        )
        body[self.token_field(ep)] = max_tokens
        if system:
            body["system"] = system
        if output_config:
            body["output_config"] = output_config
        return body

    def parse(self, data, headers):
        d = _msg(data)
        stop = d.get("stop_reason") or "end_turn"
        detail = None
        if stop == "refusal":
            sd = d.get("stop_details")
            if isinstance(sd, Mapping):
                detail = ": ".join(str(x) for x in (sd.get("category"), sd.get("explanation")) if x) or None
        text = ""
        for block in d.get("content") or []:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text") or ""
                break
        u = _usage(d)
        usage = Usage(_int(u, "input_tokens"), _int(u, "output_tokens"), _int(u, "cache_read_input_tokens"), _int(u, "cache_creation_input_tokens"))
        return ProviderResponse(text=text, stop_reason=str(stop), model=str(d.get("model") or ""), usage=usage,
                                request_id=self._request_id(d, headers), stop_detail=detail)


# ------------------------------------------------------------------ openai_chat
class OpenAIChat(Adapter):
    api = "openai_chat"
    default_path = "/v1/chat/completions"
    default_token_field = "max_tokens"

    def token_field(self, ep: EndpointConfig) -> str:
        # OpenAI's own reasoning-family models reject `max_tokens`; other OpenAI-compatible servers still expect it.
        if not ep.max_tokens_field and ep.host.lower() == OPENAI_HOST:
            return "max_completion_tokens"
        return super().token_field(ep)

    @staticmethod
    def _content(m: ProviderMessage) -> Any:
        if all(isinstance(p, TextPart) for p in m.parts) and len(m.parts) == 1:
            return m.parts[0].text  # type: ignore[union-attr]  # plain string: widest server compatibility
        out = []
        for p in m.parts:
            if isinstance(p, TextPart):
                out.append({"type": "text", "text": p.text})
            else:
                out.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
        return out

    def build(self, req, ep, *, system, native, max_tokens):
        body: dict[str, Any] = dict(ep.params)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages += [{"role": m.role, "content": self._content(m)} for m in req.messages]
        body.update(model=req.model, messages=messages)
        body[self.token_field(ep)] = max_tokens
        if native:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": _schema_name(req), "schema": sanitize_schema_for_provider(req.schema), "strict": True}}
        if req.effort is not None:
            body["reasoning_effort"] = req.effort
        return body

    def parse(self, data, headers):
        d = _msg(data)
        choices = d.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise LLMProviderError("unexpected reply from the server (is `api` correct for this endpoint?): no choices[0]")
        ch = choices[0]
        message = ch.get("message") if isinstance(ch.get("message"), Mapping) else {}
        content = message.get("content")
        if isinstance(content, list):  # some servers return typed parts
            content = "".join(str(c.get("text", "")) for c in content if isinstance(c, Mapping))
        text = content if isinstance(content, str) else ""
        finish = ch.get("finish_reason")
        detail = None
        if message.get("refusal"):
            stop, detail = "refusal", str(message["refusal"])[:500]
        else:
            stop = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal", None: "end_turn"}.get(finish, str(finish))
            if stop == "refusal":
                detail = "content_filter"
        u = _usage(d)
        usage = Usage(_int(u, "prompt_tokens"), _int(u, "completion_tokens"), _int(u.get("prompt_tokens_details"), "cached_tokens"), 0)
        return ProviderResponse(text=text, stop_reason=stop, model=str(d.get("model") or ""), usage=usage,
                                request_id=self._request_id(d, headers), stop_detail=detail)


# ------------------------------------------------------------------ openai_responses
class OpenAIResponses(Adapter):
    api = "openai_responses"
    default_path = "/v1/responses"
    default_token_field = "max_output_tokens"

    @staticmethod
    def _content(m: ProviderMessage) -> list[dict[str, Any]]:
        out = []
        for p in m.parts:
            if isinstance(p, TextPart):
                out.append({"type": "output_text" if m.role == "assistant" else "input_text", "text": p.text})
            else:
                out.append({"type": "input_image", "image_url": _data_url(p)})
        return out

    def build(self, req, ep, *, system, native, max_tokens):
        body: dict[str, Any] = dict(ep.params)
        body.update(model=req.model, input=[{"role": m.role, "content": self._content(m)} for m in req.messages])
        body[self.token_field(ep)] = max_tokens
        if system:
            body["instructions"] = system
        if native:
            text = dict(body.get("text") or {})
            text["format"] = {"type": "json_schema", "name": _schema_name(req), "schema": sanitize_schema_for_provider(req.schema), "strict": True}
            body["text"] = text
        if req.effort is not None:
            reasoning = dict(body.get("reasoning") or {})
            reasoning["effort"] = req.effort
            body["reasoning"] = reasoning
        return body

    def parse(self, data, headers):
        d = _msg(data)
        status = d.get("status")
        texts: list[str] = []
        refusal: str | None = None
        for item in d.get("output") or []:
            if isinstance(item, Mapping) and item.get("type") == "message":
                for c in item.get("content") or []:
                    if not isinstance(c, Mapping):
                        continue
                    if c.get("type") == "output_text":
                        texts.append(str(c.get("text", "")))
                    elif c.get("type") == "refusal":
                        refusal = str(c.get("refusal", ""))[:500] or "refused"
        detail = None
        if refusal is not None:
            stop, detail = "refusal", refusal
        elif status in (None, "completed"):
            stop = "end_turn"
        elif status == "incomplete":
            reason = (d.get("incomplete_details") or {}).get("reason") if isinstance(d.get("incomplete_details"), Mapping) else None
            stop = {"max_output_tokens": "max_tokens", "content_filter": "refusal"}.get(reason, f"incomplete:{reason}")
            detail = "content_filter" if stop == "refusal" else None
        else:  # failed / cancelled / queued / in_progress
            err = d.get("error")
            msg = err.get("message") if isinstance(err, Mapping) else None
            raise LLMProviderError(f"the server reported status {status!r}" + (f": {str(msg)[:300]}" if msg else ""))
        u = _usage(d)
        usage = Usage(_int(u, "input_tokens"), _int(u, "output_tokens"), _int(u.get("input_tokens_details"), "cached_tokens"), 0)
        return ProviderResponse(text="".join(texts), stop_reason=stop, model=str(d.get("model") or ""), usage=usage,
                                request_id=self._request_id(d, headers), stop_detail=detail)


def fingerprint_extra(ep: EndpointConfig) -> dict[str, Any]:
    """The endpoint settings that shape the model's output, for request fingerprints and analysis cache keys (the single
    definition used by the live provider, the replay provider and the vision settings). Never URLs, headers, timeouts or keys."""
    return {"api": ep.api, "params": ep.params, "structured_output": ep.structured_output,
            "max_tokens_cap": ep.max_tokens, "max_tokens_field": ADAPTERS[ep.api].token_field(ep)}


def bad_request_hint(msg: str, ep: EndpointConfig) -> str:
    """Extra advice for a 400 whose server message complains about the token-limit field."""
    low = msg.lower()
    if "max_tokens" in low or "max_completion_tokens" in low or "max_output_tokens" in low:
        used = ADAPTERS[ep.api].token_field(ep)
        return (f" (hint: this request sent the token limit as `{used}`; if the server wants another name, set "
                f"llm.endpoints.<name>.max_tokens_field, e.g. max_completion_tokens or max_tokens)")
    return ""


ADAPTERS: dict[str, Adapter] = {a.api: a for a in (AnthropicMessages(), OpenAIChat(), OpenAIResponses())}
