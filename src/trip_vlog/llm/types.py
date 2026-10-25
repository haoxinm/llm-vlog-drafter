"""Provider-neutral request/response types and typed errors for the LLM gateway (DESIGN §22)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union

__all__ = ["ContentPart", "EFFORTS", "Effort", "IMAGE_MEDIA_TYPES", "ImagePart", "LLMAuthError", "LLMBadRequestError", "LLMConfigError", "LLMConnectionError", "LLMError", "LLMInputError", "LLMNotFoundError", "LLMPermissionError", "LLMProviderError", "LLMRateLimitError", "LLMRefusalError", "LLMRequest", "LLMResponse", "LLMSchemaError", "LLMStoreError", "LLMTruncatedError", "PreparedImage", "ProviderMessage", "ProviderRequest", "ProviderResponse", "ReplayMissError", "TextPart", "Usage"]

Effort = Literal["low", "medium", "high"]
EFFORTS = ("low", "medium", "high")
IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


# ---------------------------------------------------------------- errors
class LLMError(Exception):
    """Base class of every gateway error. Messages are user-facing and never contain credentials."""


class LLMConfigError(LLMError):
    """Missing model id, missing optional SDK, forbidden provider (local_only), bad mode."""


class LLMInputError(LLMError):
    """The request itself is unusable (unreadable/unsupported image, invalid schema, ...)."""


class LLMRefusalError(LLMError):
    """The model refused (stop_reason == 'refusal'). `detail` carries the provider's explanation, if any."""

    def __init__(self, message: str, *, detail: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.request_id = request_id


class LLMTruncatedError(LLMError):
    """Output hit max_tokens (or the context window); the JSON would be cut off, so it is never used."""

    def __init__(self, message: str, *, stop_reason: str, request_id: str | None = None):
        super().__init__(message)
        self.stop_reason = stop_reason
        self.request_id = request_id


class LLMSchemaError(LLMError):
    """The output still violated the schema after all schema retries."""

    def __init__(self, message: str, *, errors: list[str], attempts: int, raw_text: str = ""):
        super().__init__(message)
        self.errors = errors
        self.attempts = attempts
        self.raw_text = raw_text


class LLMProviderError(LLMError):
    """The provider API failed. `status_code`/`request_id` are set when the provider reported them."""

    def __init__(self, message: str, *, status_code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class LLMBadRequestError(LLMProviderError): ...
class LLMAuthError(LLMProviderError): ...
class LLMPermissionError(LLMProviderError): ...
class LLMNotFoundError(LLMProviderError): ...
class LLMRateLimitError(LLMProviderError): ...
class LLMConnectionError(LLMProviderError): ...


class ReplayMissError(LLMError):
    """Replay mode has no stored response for this request fingerprint (fail loudly, never call the network)."""

    def __init__(self, message: str, *, fingerprint: str, template: str):
        super().__init__(message)
        self.fingerprint = fingerprint
        self.template = template


class LLMStoreError(LLMError):
    """A stored response file is unreadable or malformed."""


# ---------------------------------------------------------------- request
@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    """A local image. The gateway reads it, downsizes/re-encodes it and base64-encodes it at the wire."""

    path: Path
    media_type: str = "image/jpeg"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise LLMInputError(f"unsupported image media type {self.media_type!r} (allowed: {', '.join(IMAGE_MEDIA_TYPES)})")


ContentPart = Union[TextPart, ImagePart]


@dataclass(frozen=True)
class LLMRequest:
    template_id: str
    template_version: str
    system: str
    parts: tuple[ContentPart, ...]
    schema: dict[str, Any]
    max_tokens: int
    effort: Effort | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "template_version", str(self.template_version))
        if not self.template_id:
            raise LLMInputError("template_id must not be empty")
        if not self.parts:
            raise LLMInputError("a request needs at least one content part")
        if not isinstance(self.schema, dict):
            raise LLMInputError("schema must be a JSON-schema dict")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
            raise LLMInputError("max_tokens must be a positive integer")
        if self.effort is not None and self.effort not in EFFORTS:
            raise LLMInputError(f"effort must be one of {EFFORTS} or None, got {self.effort!r}")

    @classmethod
    def from_template(cls, template: Any, parts: list[ContentPart] | tuple[ContentPart, ...], *, schema: dict[str, Any],
                      max_tokens: int, effort: Effort | None = None) -> "LLMRequest":
        """Build a request whose id/version/system text come from a `prompts.PromptTemplate`."""
        return cls(template.id, str(template.version), template.text, tuple(parts), schema, max_tokens, effort)


# ---------------------------------------------------------------- response
@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_creation_tokens + other.cache_creation_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Usage":
        return cls(*(int(d.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")))


@dataclass(frozen=True)
class LLMResponse:
    parsed: Any                 # JSON validated against the request schema
    text: str                   # raw text the model returned
    model: str                  # model id actually used, as reported by the provider response
    stop_reason: str
    usage: Usage                # summed over all attempts (for a cached/replayed result: the original call's usage)
    request_id: str | None
    latency_ms: float
    attempts: int               # provider calls made (1 = first answer was valid); 0 for a cache hit
    fingerprint: str
    cached: bool = False        # served from the workspace response cache (no provider call)


# ---------------------------------------------------------------- provider-level types
@dataclass(frozen=True)
class PreparedImage:
    media_type: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class ProviderMessage:
    role: Literal["user", "assistant"]
    parts: tuple[Union[TextPart, PreparedImage], ...]


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    messages: tuple[ProviderMessage, ...]
    schema: dict[str, Any]       # the FULL schema; providers sanitize their own copy if they need to
    max_tokens: int
    effort: Effort | None
    fingerprint: str
    template_id: str
    template_version: str


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    stop_reason: str
    model: str
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None
    stop_detail: str | None = None   # e.g. refusal category / explanation
