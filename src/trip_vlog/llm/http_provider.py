"""Generic HTTP provider: one configured endpoint profile + one wire-format adapter (stdlib transport, no SDK).

Transport-level retries (429/5xx/connection) live here with exponential backoff and Retry-After; schema retries live in
the gateway. Credentials are read from environment variables named in the profile and never logged.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Mapping

from trip_vlog.config import EndpointConfig
from trip_vlog.llm.adapters import ADAPTERS, bad_request_hint, error_message, fingerprint_extra
from trip_vlog.llm.transport import HttpRequest, Transport, TransportConnectionError, TransportTimeout, UrllibTransport
from trip_vlog.llm.types import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMInputError,
    LLMNotFoundError,
    LLMPermissionError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    PreparedImage,
    ProviderRequest,
    ProviderResponse,
)
from trip_vlog.logging_utils import sanitize

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
MAX_BACKOFF = 30.0
MAX_RETRY_AFTER = 60.0
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

JSON_PROMPT_INSTRUCTION = (
    "Respond with ONLY one JSON value that conforms to the JSON schema below. Do not add commentary, explanations or "
    "markdown code fences.\nJSON schema:\n"
)


def expand_env(value: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        v = env.get(m.group(1))
        if v is None:
            raise LLMConfigError(f"header value refers to environment variable {m.group(1)}, which is not set")
        return v

    return _VAR_RE.sub(sub, value)


class HttpProvider:
    supports_schema_retry = True

    def __init__(
        self,
        profile: str,
        endpoint: EndpointConfig,
        *,
        model: str,
        env: Mapping[str, str] | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        env = os.environ if env is None else env
        if not model:
            raise LLMConfigError(
                f"no model configured for endpoint {profile!r}: set llm.endpoints.{profile}.model"
                + (f" or the {endpoint.model_env} environment variable" if endpoint.model_env else ""))
        self.endpoint = profile
        self.ep = endpoint
        self.model = model
        self.name = f"http:{endpoint.api}"
        self.is_local = endpoint.is_local
        self.fingerprint_extra = fingerprint_extra(endpoint)  # no URLs/headers/keys/timeouts
        self.adapter = ADAPTERS[endpoint.api]
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        key = None
        if endpoint.api_key_env:
            key = (env.get(endpoint.api_key_env) or "").strip()
            if not key:
                raise LLMConfigError(
                    f"environment variable {endpoint.api_key_env} (api_key_env of endpoint {profile!r}) is not set; "
                    "export it, or set api_key_env: null for a server without authentication")
        headers = self.adapter.headers(key)
        extra = {k.lower(): expand_env(v, env) for k, v in endpoint.headers.items()}
        headers.update(extra)
        self._headers = headers
        self._secrets = [x for x in [key, *extra.values()] if x and len(x) >= 4]
        self.url = endpoint.base_url.rstrip("/") + (endpoint.path or self.adapter.default_path)  # type: ignore[union-attr]

    # -- one model call -------------------------------------------------
    def build_body(self, request: ProviderRequest) -> dict[str, Any]:
        ep = self.ep
        system = request.system
        if ep.structured_output == "json_prompt":
            system = (system + "\n\n" if system else "") + JSON_PROMPT_INSTRUCTION + json.dumps(request.schema, sort_keys=True, separators=(",", ":"))
        if not ep.supports_images and any(isinstance(p, PreparedImage) for m in request.messages for p in m.parts):
            raise LLMInputError(f"endpoint {self.endpoint!r} has supports_images: false but the request contains images; point llm.vision at a vision-capable profile")
        return self.adapter.build(request, ep, system=system, native=ep.structured_output == "native",
                                  max_tokens=min(request.max_tokens, ep.max_tokens))

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        payload = json.dumps(self.build_body(request), ensure_ascii=False).encode("utf-8")
        http = HttpRequest("POST", self.url, self._headers, payload, self.ep.timeout_seconds)
        resp = None
        for attempt in range(self.ep.max_retries + 1):
            last = attempt == self.ep.max_retries
            try:
                resp = self._transport.send(http)
            except TransportTimeout:
                raise LLMTimeoutError(f"no reply from endpoint {self.endpoint!r} within {self.ep.timeout_seconds:g}s (timeout_seconds)") from None
            except TransportConnectionError as e:
                if last:
                    raise LLMConnectionError(f"cannot reach endpoint {self.endpoint!r} ({self.ep.host}) after {attempt + 1} attempt(s): {self._clean(e)}") from None
                self._sleep(self._backoff(attempt, None))
                continue
            if resp.status in RETRYABLE_STATUS and not last:
                if self._too_long(resp.headers):
                    break  # the server wants a longer pause than we are willing to wait: retrying now would only fail again
                self._sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            break
        assert resp is not None
        try:
            data: Any = json.loads(resp.body.decode("utf-8")) if resp.body else None
        except ValueError:
            data = None
        if 200 <= resp.status < 300:
            if data is None:
                raise LLMProviderError("the server returned a non-JSON reply", status_code=resp.status)
            try:
                out = self.adapter.parse(data, resp.headers)
            except LLMError:
                raise
            except (TypeError, ValueError, AttributeError, KeyError) as e:
                raise LLMProviderError(f"unexpected reply shape from endpoint {self.endpoint!r} ({type(e).__name__}); is `api` correct?", status_code=resp.status) from None
            return out
        raise self._http_error(resp.status, error_message(data, resp.body), resp.headers)

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        """Seconds from a numeric Retry-After header (the HTTP-date form is ignored)."""
        try:
            v = float(headers.get("retry-after") or "")
        except ValueError:
            return None
        return v if v == v and v >= 0 else None

    def _too_long(self, headers: Mapping[str, str]) -> bool:
        ra = self._retry_after(headers)
        return ra is not None and ra > MAX_RETRY_AFTER

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), MAX_RETRY_AFTER))
            except ValueError:
                pass  # HTTP-date form: fall back to exponential backoff
        return min(0.5 * 2**attempt, MAX_BACKOFF)

    def _key_advice(self) -> str:
        if self.ep.api_key_env:
            return f"check the key in ${self.ep.api_key_env}"
        return "no API key is configured for this endpoint (api_key_env is null); set api_key_env if the server requires one"

    def _clean(self, text: object) -> str:
        return sanitize(text, redactions=[(s, "***") for s in self._secrets])

    def _http_error(self, status: int, msg: str, headers: Mapping[str, str]) -> LLMProviderError:
        msg = self._clean(msg)
        rid = headers.get("request-id") or headers.get("x-request-id")
        kw = dict(status_code=status, request_id=rid)
        if status in (400, 422):
            return LLMBadRequestError(f"endpoint {self.endpoint!r} rejected the request ({status}): {msg}{bad_request_hint(msg, self.ep)}", **kw)
        if status == 401:
            return LLMAuthError(f"authentication failed (401) at endpoint {self.endpoint!r}: " + self._key_advice(), **kw)
        if status == 403:
            return LLMPermissionError(f"permission denied (403) at endpoint {self.endpoint!r}: {msg}", **kw)
        if status == 404:
            return LLMNotFoundError(f"not found (404) at {self.url}: check base_url/path and the model id: {msg}", **kw)
        ra = self._retry_after(headers)
        if ra is not None and ra > MAX_RETRY_AFTER:
            wait = (f"the server asked to wait {ra:g}s (Retry-After), longer than the {MAX_RETRY_AFTER:g}s this tool waits; try again later")
            if status == 429:
                return LLMRateLimitError(f"rate limited (429) at endpoint {self.endpoint!r}; {wait}: {msg}", **kw)
            return LLMProviderError(f"endpoint {self.endpoint!r} returned HTTP {status}; {wait}: {msg}", **kw)
        if status == 429:
            return LLMRateLimitError(f"rate limited (429) at endpoint {self.endpoint!r}; still failing after {self.ep.max_retries} retries: {msg}", **kw)
        return LLMProviderError(f"endpoint {self.endpoint!r} returned HTTP {status}: {msg}", **kw)
