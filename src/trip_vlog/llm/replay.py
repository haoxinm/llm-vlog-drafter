"""Replay provider (DESIGN §22): returns stored, previously validated responses; never touches the network.

Recording is done by the gateway (`Gateway(..., recorder=ResponseStore(dir))`) so that only the final,
schema-validated answer of a request is stored, keyed by the request fingerprint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from trip_vlog.llm.store import ResponseStore, usage_of
from trip_vlog.llm.types import LLMStoreError, ProviderRequest, ProviderResponse, ReplayMissError

LLMMode = Literal["live", "record", "replay"]
LLM_MODES = ("live", "record", "replay")


class ReplayProvider:
    name = "replay"
    supports_schema_retry = False

    is_local = True  # nothing leaves the machine

    def __init__(self, store: ResponseStore | Path | str, *, model: str = "", endpoint: str = "replay",
                 fingerprint_extra: dict | None = None):
        self.store = store if isinstance(store, ResponseStore) else ResponseStore(store)
        self.model = model
        self.endpoint = endpoint
        # must equal the live provider's, so a record made against one host replays under the same profile
        self.fingerprint_extra = fingerprint_extra or {}

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        rec = self.store.get(request.fingerprint)
        if rec is None:
            hint = ""
            if not request.model:
                hint = " (no model id is configured; fingerprints include the model, so set llm.endpoints.<name>.model or its model_env variable to the value used when recording)"
            raise ReplayMissError(
                f"no recorded LLM response for template {request.template_id} v{request.template_version} "
                f"(fingerprint {request.fingerprint}, model {request.model!r}) in {self.store.directory}{hint}. "
                "Re-record with TRIP_VLOG_LLM_MODE=record against a real endpoint.",
                fingerprint=request.fingerprint, template=f"{request.template_id}.v{request.template_version}")
        r = rec["response"]
        try:
            return ProviderResponse(
                text=r["text"], stop_reason=str(r.get("stop_reason") or "end_turn"), model=str(r.get("model") or request.model),
                usage=usage_of(rec), request_id=r.get("request_id"))
        except (KeyError, TypeError, ValueError) as e:
            raise LLMStoreError(f"stored response {request.fingerprint}.json is malformed ({e})") from None
