"""Transcript value types and the error hierarchy shared by the speech backends."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal


class SpeechError(Exception):
    """Base class. `status_code` is set for HTTP failures."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SpeechConfigError(SpeechError): ...        # unusable configuration (missing model, key, executable); fatal for the run
class SpeechAuthError(SpeechError): ...          # 401/403; fatal for the run
class SpeechTimeoutError(SpeechError): ...
class SpeechConnectionError(SpeechError): ...
class SpeechBadRequestError(SpeechError): ...    # 400/413/415/422: this audio was rejected
class SpeechNotFoundError(SpeechError): ...      # 404: wrong base_url/path/model
class SpeechRateLimitError(SpeechError): ...
class SpeechProviderError(SpeechError): ...      # 5xx, unparsable reply
class SpeechCommandError(SpeechError): ...       # the local command failed, timed out or wrote nothing usable

FATAL = (SpeechConfigError, SpeechAuthError, SpeechNotFoundError)


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    confidence: float | None = None          # exp(avg_logprob) when the backend reports it: a geometric-mean token probability
    avg_logprob: float | None = None
    no_speech_prob: float | None = None

    def shifted(self, offset: float) -> "TranscriptSegment":
        return TranscriptSegment(round(self.start + offset, 3), round(self.end + offset, 3), self.text, self.confidence,
                                 self.avg_logprob, self.no_speech_prob)


@dataclass
class Transcription:
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
    timestamps: Literal["segment", "coarse"] = "segment"   # coarse: the backend returned plain text, one segment spans the clip
    meta: dict[str, Any] = field(default_factory=dict)


def clean_float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)
