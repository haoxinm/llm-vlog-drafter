"""Provider-neutral LLM gateway: schema-validated completions with retry, cache, record hook and logging."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from trip_vlog.llm.images import DEFAULT_MAX_SIDE, DEFAULT_QUALITY, prepare_image
from trip_vlog.llm.schema import check_schema, validation_errors
from trip_vlog.llm.store import ResponseStore, usage_of
from trip_vlog.llm.types import (
    ImagePart,
    LLMError,
    LLMProviderError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMSchemaError,
    LLMStoreError,
    LLMTruncatedError,
    PreparedImage,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    TextPart,
    Usage,
)
from trip_vlog.logging_utils import RunLogger, Status
from trip_vlog.paths import nfc

_log = logging.getLogger("trip_vlog.llm")

FINGERPRINT_VERSION = 1
_OK_STOPS = {"end_turn", "stop_sequence"}
_TRUNCATED_STOPS = {"max_tokens", "model_context_window_exceeded"}


@runtime_checkable
class Provider(Protocol):
    """One model call. No retry/validation logic here: the gateway owns the schema-retry loop."""

    name: str
    model: str                      # model id requested ("" for a replay provider with no configured model)
    supports_schema_retry: bool     # False for replay: a stored answer cannot change on a second call
    # Optional attributes read with getattr defaults:
    #   fingerprint_extra: dict  output-relevant settings (api kind, params); never URLs/headers/keys
    #   endpoint: str            profile name, for logs
    #   is_local: bool           True when the endpoint is loopback / nothing leaves the machine

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


# ------------------------------------------------------------------ fingerprint
def _norm(text: str) -> str:
    return nfc(text.replace("\r\n", "\n").replace("\r", "\n"))


def request_summary(req: LLMRequest, prepared: list[TextPart | PreparedImage], model: str,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The fingerprint inputs. Images appear as SOURCE-byte hashes + preparation parameters (never paths or bytes, and
    not the re-encoded bytes, which can differ across OSes). An image with an explicit `identity` (ffmpeg-made frames, whose
    bytes vary with the ffmpeg build) is represented by that identity instead of its bytes. `extra` is the provider's output-relevant settings
    (api kind, params) and never contains URLs, headers, timeouts or keys."""
    parts: list[dict[str, str]] = []
    for p in prepared:
        if isinstance(p, TextPart):
            parts.append({"type": "text", "text": _norm(p.text)})
        else:
            ident = {"identity": p.identity} if p.identity else {"source_sha256": p.source_sha256}
            parts.append({"type": "image", **ident, "max_side": p.max_side, "quality": p.quality, "prep_version": p.prep_version})
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "template_id": req.template_id,
        "template_version": req.template_version,
        "system": _norm(req.system),
        "schema": req.schema,
        "parts": parts,
        "model": model,
        "max_tokens": req.max_tokens,
        "effort": req.effort,
        "provider": extra or {},
    }


def fingerprint_of(summary: dict[str, Any]) -> str:
    blob = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ gateway
class Gateway:
    def __init__(
        self,
        provider: Provider,
        *,
        schema_retries: int = 2,
        image_max_side: int = DEFAULT_MAX_SIDE,
        image_quality: int = DEFAULT_QUALITY,
        image_cache_dir: Path | None = None,
        cache: ResponseStore | None = None,
        recorder: ResponseStore | None = None,
        logger: RunLogger | None = None,
    ):
        if schema_retries < 0:
            raise ValueError("schema_retries must be >= 0")
        self.provider = provider
        self.schema_retries = schema_retries
        self.image_max_side = image_max_side
        self.image_quality = image_quality
        self.image_cache_dir = image_cache_dir
        self.cache = cache
        self.recorder = recorder
        self.logger = logger

    @property
    def model(self) -> str:
        return self.provider.model

    # -- public ---------------------------------------------------------
    def fingerprint(self, req: LLMRequest) -> str:
        return self._prepare(req)[1]

    def complete(self, req: LLMRequest) -> LLMResponse:
        t0 = time.perf_counter()
        fp = None
        try:
            check_schema(req.schema)
            prepared, fp, summary = self._prepare(req)
            hit = self._cache_lookup(req, fp)
            if hit is not None:
                resp = LLMResponse(**{**hit, "latency_ms": _ms(t0)})
                if self.recorder is not None and not self.recorder.has(fp):
                    self.recorder.put(fp, summary, resp)
                self._emit(Status.SUCCEEDED, req, resp, cache="hit")
                return resp
            resp = self._call(req, prepared, fp, t0)
            if self.cache is not None:
                self._safe_put(self.cache, fp, summary, resp)
            if self.recorder is not None and self.recorder is not self.cache:
                self.recorder.put(fp, summary, resp)  # a failing recorder must be loud
            self._emit(Status.SUCCEEDED, req, resp, cache="miss" if self.cache is not None else None)
            return resp
        except LLMError as e:
            self._emit_error(req, fp, e, _ms(t0))
            raise

    # -- internals ------------------------------------------------------
    def _prepare(self, req: LLMRequest) -> tuple[list[TextPart | PreparedImage], str, dict[str, Any]]:
        prepared: list[TextPart | PreparedImage] = []
        for p in req.parts:
            if isinstance(p, ImagePart):
                prepared.append(prepare_image(p, max_side=self.image_max_side, quality=self.image_quality,
                                              cache_dir=self.image_cache_dir))
            else:
                prepared.append(p)
        summary = request_summary(req, prepared, self.model, getattr(self.provider, "fingerprint_extra", None))
        return prepared, fingerprint_of(summary), summary

    def _cache_lookup(self, req: LLMRequest, fp: str) -> dict[str, Any] | None:
        if self.cache is None:
            return None
        try:
            rec = self.cache.get(fp)
        except LLMStoreError as e:
            _log.warning("ignoring unreadable cache entry: %s", e)
            return None
        if rec is None:
            return None
        r = rec["response"]
        if validation_errors(r.get("parsed"), req.schema, limit=1):
            return None  # stale or tampered entry: recompute
        return dict(parsed=r["parsed"], text=r["text"], model=str(r.get("model", "")), stop_reason=str(r.get("stop_reason", "end_turn")),
                    usage=usage_of(rec), request_id=r.get("request_id"), latency_ms=0.0, attempts=0, fingerprint=fp, cached=True)

    @staticmethod
    def _safe_put(store: ResponseStore, fp: str, summary: dict[str, Any], resp: LLMResponse) -> None:
        try:
            store.put(fp, summary, resp)
        except OSError as e:  # the cache is an optimisation: never fail a good answer because it cannot be stored
            _log.warning("could not write response cache: %s", e)

    def _call(self, req: LLMRequest, prepared: list[TextPart | PreparedImage], fp: str, t0: float) -> LLMResponse:
        max_attempts = 1 + (self.schema_retries if self.provider.supports_schema_retry else 0)
        messages = [ProviderMessage("user", tuple(prepared))]
        usage = Usage()
        errors: list[str] = []
        text = ""
        for attempt in range(1, max_attempts + 1):
            preq = ProviderRequest(
                model=self.model, system=req.system, messages=tuple(messages), schema=req.schema, max_tokens=req.max_tokens,
                effort=req.effort, fingerprint=fp, template_id=req.template_id, template_version=req.template_version,
            )
            presp = self.provider.complete(preq)
            usage = usage + presp.usage
            text = presp.text
            if presp.stop_reason == "refusal":
                raise LLMRefusalError(
                    f"the model refused the request ({req.template_id} v{req.template_version})"
                    + (f": {presp.stop_detail}" if presp.stop_detail else ""),
                    detail=presp.stop_detail, request_id=presp.request_id)
            if presp.stop_reason in _TRUNCATED_STOPS:
                raise LLMTruncatedError(
                    f"output stopped with {presp.stop_reason!r} before the JSON was complete "
                    f"(max_tokens={req.max_tokens}); raise max_tokens or shrink the request",
                    stop_reason=presp.stop_reason, request_id=presp.request_id)
            if presp.stop_reason not in _OK_STOPS:
                raise LLMProviderError(f"unexpected stop reason {presp.stop_reason!r}", request_id=presp.request_id)
            parsed, errors = _parse_and_validate(text, req.schema)
            if not errors:
                return LLMResponse(parsed=parsed, text=text, model=presp.model, stop_reason=presp.stop_reason, usage=usage,
                                   request_id=presp.request_id, latency_ms=_ms(t0), attempts=attempt, fingerprint=fp)
            if attempt < max_attempts:
                # Retry with the validation errors only: the original media stays in the first turn, none is added.
                messages.append(ProviderMessage("assistant", (TextPart(text or "(empty reply)"),)))
                messages.append(ProviderMessage("user", (TextPart(_retry_prompt(errors)),)))
        raise LLMSchemaError(
            f"output did not match the schema after {max_attempts} attempt(s): " + "; ".join(errors[:3]),
            errors=errors, attempts=max_attempts, raw_text=text)

    # -- logging --------------------------------------------------------
    def _emit(self, status: Status, req: LLMRequest, resp: LLMResponse, *, cache: str | None) -> None:
        fields: dict[str, Any] = dict(
            provider=self.provider.name, endpoint=getattr(self.provider, "endpoint", None), local=getattr(self.provider, "is_local", None),
            prompt_id=req.template_id, prompt_version=req.template_version,
            model=resp.model, fingerprint=resp.fingerprint, attempts=resp.attempts, request_id=resp.request_id,
            stop_reason=resp.stop_reason,
        )
        if not resp.cached:  # a cache hit costs nothing: do not double-count the original usage
            u = resp.usage
            fields.update(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                          cache_read_tokens=u.cache_read_tokens, cache_creation_tokens=u.cache_creation_tokens)
        _log.info("llm %s v%s model=%s attempts=%s cache=%s", req.template_id, req.template_version, resp.model, resp.attempts, cache)
        if self.logger is not None:
            self.logger.event("llm", status, cache=cache, duration_ms=resp.latency_ms, **fields)

    def _emit_error(self, req: LLMRequest, fp: str | None, err: LLMError, duration_ms: float) -> None:
        _log.warning("llm %s v%s failed: %s", req.template_id, req.template_version, type(err).__name__)
        if self.logger is not None:
            self.logger.event("llm", Status.FAILED, duration_ms=duration_ms, provider=self.provider.name, endpoint=getattr(self.provider, "endpoint", None),
                              local=getattr(self.provider, "is_local", None), prompt_id=req.template_id,
                              prompt_version=req.template_version, fingerprint=fp, error=f"{type(err).__name__}: {err}",
                              request_id=getattr(err, "request_id", None))


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def extract_json(text: str) -> Any:
    """Parse model text as JSON: the whole text, a fenced block, or else the first JSON object/array embedded in prose.

    Raises ValueError when nothing parses.
    """
    body = text.strip()
    try:
        return json.loads(body)
    except ValueError:
        pass
    fence = re.search(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n?```", body, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except ValueError:
            body = fence.group(1)
    dec = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", body):
        try:
            return dec.raw_decode(body, m.start())[0]
        except ValueError:
            continue
    raise ValueError("no JSON object found in the reply")


def _parse_and_validate(text: str, schema: dict[str, Any]) -> tuple[Any, list[str]]:
    try:
        parsed = extract_json(text)
    except ValueError as e:
        return None, [f"reply is not valid JSON: {e}"]
    return parsed, validation_errors(parsed, schema)


def _retry_prompt(errors: list[str]) -> str:
    listing = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous reply did not satisfy the required JSON schema. Validation errors:\n"
        f"{listing}\n"
        "Reply again with only the corrected JSON object that matches the schema exactly."
    )
