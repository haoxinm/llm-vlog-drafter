"""`openai_transcriptions` backend: POST multipart/form-data to `{base_url}{path}` (stdlib only, no SDK).

The request follows the public OpenAI documentation for audio transcriptions (fields `file`, `model`, `response_format`,
`timestamp_granularities[]`, optional `language`, extra `params`), which OpenAI-compatible local servers (faster-whisper-server,
whisper.cpp server, LocalAI, ...) imitate. Per public docs; verify against your server with `trip-vlog llm-check --speech`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from trip_vlog.config import SpeechEndpointConfig
from trip_vlog.llm.adapters import error_message
from trip_vlog.llm.http_provider import MAX_BACKOFF, MAX_RETRY_AFTER, RETRYABLE_STATUS, expand_env
from trip_vlog.llm.transport import HttpRequest, Transport, TransportConnectionError, TransportTimeout, UrllibTransport
from trip_vlog.llm.types import LLMConfigError
from trip_vlog.logging_utils import sanitize
from trip_vlog.speech.multipart import encode_multipart
from trip_vlog.speech.parse import parse_response_body
from trip_vlog.speech.types import (
    SpeechAuthError,
    SpeechBadRequestError,
    SpeechConfigError,
    SpeechConnectionError,
    SpeechNotFoundError,
    SpeechProviderError,
    SpeechRateLimitError,
    SpeechTimeoutError,
    Transcription,
)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
CONTENT_TYPES = {".wav": "audio/wav", ".flac": "audio/flac"}


def resolve_speech_model(ep: SpeechEndpointConfig, env: Mapping[str, str] | None = None) -> str | None:
    if ep.model:
        return ep.model
    env = os.environ if env is None else env
    return ((env.get(ep.model_env) or "").strip() or None) if ep.model_env else None


def form_fields(ep: SpeechEndpointConfig, model: str, language: str | None) -> list[tuple[str, str]]:
    """Ordered text fields of the request (everything except `file`)."""
    fields: list[tuple[str, str]] = [("model", model), ("response_format", ep.response_format)]
    if ep.response_format == "verbose_json":
        fields += [("timestamp_granularities[]", g) for g in ep.timestamp_granularities]
    if language:
        fields.append(("language", language))
    for k, v in ep.params.items():
        for item in (v if isinstance(v, list) else [v]):
            fields.append((k, ("true" if item else "false") if isinstance(item, bool) else str(item)))
    return fields


class OpenAITranscriptions:
    name = "openai_transcriptions"

    def __init__(self, endpoint: SpeechEndpointConfig, *, language: str | None = None, env: Mapping[str, str] | None = None,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep):
        env = os.environ if env is None else env
        self.ep, self.language = endpoint, language
        self.model = resolve_speech_model(endpoint, env)
        if not self.model:
            raise SpeechConfigError("no transcription model configured: set speech.endpoint.model"
                                    + (f" or the {endpoint.model_env} environment variable" if endpoint.model_env else ""))
        key = None
        if endpoint.api_key_env:
            key = (env.get(endpoint.api_key_env) or "").strip()
            if not key:
                raise SpeechConfigError(f"environment variable {endpoint.api_key_env} (speech.endpoint.api_key_env) is not set; export it, "
                                        "or set api_key_env: null for a server without authentication")
        headers = {"accept": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        try:
            extra = {k.lower(): expand_env(v, env) for k, v in endpoint.headers.items()}
        except LLMConfigError as e:
            raise SpeechConfigError(str(e)) from None
        headers.update(extra)
        self._headers = headers
        self._secrets = [x for x in [key, *extra.values()] if x and len(x) >= 4]
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self.url = endpoint.url
        self.is_local = endpoint.is_local

    def build_body(self, audio: bytes, filename: str, content_type: str, boundary: str | None = None) -> tuple[bytes, str]:
        return encode_multipart(form_fields(self.ep, self.model or "", self.language), file_field="file", filename=filename,
                                content=audio, content_type=content_type, boundary=boundary)

    def transcribe(self, audio_path: Path, duration: float | None) -> Transcription:
        size = audio_path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise SpeechBadRequestError(f"audio file is {size // (1024 * 1024)} MB, over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit; lower speech.chunk_seconds")
        audio = audio_path.read_bytes()
        ctype = CONTENT_TYPES.get(audio_path.suffix.lower(), "application/octet-stream")
        body, content_type = self.build_body(audio, "audio" + audio_path.suffix.lower(), ctype)
        headers = {**self._headers, "content-type": content_type}
        req = HttpRequest("POST", self.url, headers, body, self.ep.timeout_seconds)
        resp = None
        for attempt in range(self.ep.max_retries + 1):
            last = attempt == self.ep.max_retries
            try:
                resp = self._transport.send(req)
            except TransportTimeout:
                raise SpeechTimeoutError(f"no reply from the transcription endpoint within {self.ep.timeout_seconds:g}s (speech.endpoint.timeout_seconds)") from None
            except TransportConnectionError as e:
                if last:
                    raise SpeechConnectionError(f"cannot reach the transcription endpoint ({self.ep.host}) after {attempt + 1} attempt(s): {self._clean(e)}") from None
                self._sleep(self._backoff(attempt, None))
                continue
            if resp.status in RETRYABLE_STATUS and not last:
                ra = self._retry_after(resp.headers)
                if ra is not None and ra > MAX_RETRY_AFTER:
                    break  # the server wants a longer pause than we will wait: retrying now would only fail again
                self._sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            break
        assert resp is not None
        if 200 <= resp.status < 300:
            return parse_response_body(resp.body, self.ep.response_format, duration)
        raise self._http_error(resp.status, resp.body, resp.headers)

    # ------------------------------------------------------------------
    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        try:
            v = float(headers.get("retry-after") or "")
        except ValueError:
            return None
        return v if v == v and v >= 0 else None

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), MAX_RETRY_AFTER))
            except ValueError:
                pass
        return min(0.5 * 2**attempt, MAX_BACKOFF)

    def _key_advice(self) -> str:
        if self.ep.api_key_env:
            return f"check the key in ${self.ep.api_key_env}"
        return "no API key is configured (speech.endpoint.api_key_env is null); set it if the server requires one"

    def _clean(self, text: object) -> str:
        return sanitize(text, redactions=[(s, "***") for s in self._secrets])

    def _http_error(self, status: int, raw: bytes, headers: Mapping[str, str] | None = None) -> Exception:
        try:
            data: Any = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            data = None
        msg = self._clean(error_message(data, raw))
        if status in (400, 413, 415, 422):
            return SpeechBadRequestError(f"the transcription endpoint rejected the request ({status}): {msg}", status_code=status)
        if status in (401, 403):
            return SpeechAuthError(f"authentication/permission failed ({status}) at the transcription endpoint: {self._key_advice()}: {msg}", status_code=status)
        if status == 404:
            return SpeechNotFoundError(f"not found (404) at {self.url}: check speech.endpoint.base_url/path and the model name: {msg}", status_code=status)
        ra = self._retry_after(headers or {})
        if ra is not None and ra > MAX_RETRY_AFTER:
            wait = (f"the server asked to wait {ra:g}s (Retry-After), longer than the {MAX_RETRY_AFTER:g}s this tool waits; try again later")
            if status == 429:
                return SpeechRateLimitError(f"rate limited (429) by the transcription endpoint; {wait}: {msg}", status_code=status)
            return SpeechProviderError(f"the transcription endpoint returned HTTP {status}; {wait}: {msg}", status_code=status)
        if status == 429:
            return SpeechRateLimitError(f"rate limited (429) by the transcription endpoint; still failing after {self.ep.max_retries} retries: {msg}", status_code=status)
        return SpeechProviderError(f"the transcription endpoint returned HTTP {status}: {msg}", status_code=status)
