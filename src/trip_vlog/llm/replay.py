"""Replay provider (DESIGN §22): returns stored, previously validated responses; never touches the network.

Recording is done by the gateway (`Gateway(..., recorder=ResponseStore(dir))`) so that only the final,
schema-validated answer of a request is stored, keyed by the request fingerprint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from trip_vlog.llm.store import ResponseStore, usage_of
from trip_vlog.llm.types import LLMStoreError, ProviderRequest, ProviderResponse, ReplayMissError

LLMMode = Literal["replay", "record", "off"]
LLM_MODES = ("replay", "record", "off")


class ReplayProvider:
    name = "replay"
    supports_schema_retry = False

    def __init__(self, store: ResponseStore | Path | str, *, model: str = ""):
        self.store = store if isinstance(store, ResponseStore) else ResponseStore(store)
        self.model = model

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        rec = self.store.get(request.fingerprint)
        if rec is None:
            hint = ""
            if not request.model:
                hint = " (no model id is configured; fingerprints include the model, so set media.llm_model or TRIP_VLOG_LLM_MODEL to the value used when recording)"
            raise ReplayMissError(
                f"no recorded LLM response for template {request.template_id} v{request.template_version} "
                f"(fingerprint {request.fingerprint}, model {request.model!r}) in {self.store.directory}{hint}. "
                "Re-record with TRIP_VLOG_LLM_MODE=record and a real provider.",
                fingerprint=request.fingerprint, template=f"{request.template_id}.v{request.template_version}")
        r = rec["response"]
        try:
            return ProviderResponse(
                text=r["text"], stop_reason=str(r.get("stop_reason") or "end_turn"), model=str(r.get("model") or request.model),
                usage=usage_of(rec), request_id=r.get("request_id"))
        except (KeyError, TypeError, ValueError) as e:
            raise LLMStoreError(f"stored response {request.fingerprint}.json is malformed ({e})") from None
