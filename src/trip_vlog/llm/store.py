"""On-disk response store: one JSON file per request fingerprint.

Used both as the workspace response cache (DESIGN §7.5) and as the record/replay fixture store (§22).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trip_vlog.llm.types import LLMResponse, LLMStoreError, Usage
from trip_vlog.state import atomic_write_json

STORE_FORMAT = 1


class ResponseStore:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    def path_for(self, fingerprint: str) -> Path:
        return self.directory / f"{fingerprint}.json"

    def has(self, fingerprint: str) -> bool:
        return self.path_for(fingerprint).is_file()

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        """Stored record, or None when absent. Raises LLMStoreError for a malformed file."""
        path = self.path_for(fingerprint)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise LLMStoreError(f"cannot read stored response {path.name}: {e.strerror or e}") from None
        try:
            rec = json.loads(text)
            if not (isinstance(rec, dict) and rec.get("format") == STORE_FORMAT and rec.get("fingerprint") == fingerprint
                    and isinstance(rec.get("response"), dict) and isinstance(rec["response"].get("text"), str)):
                raise ValueError("unexpected structure")
        except ValueError as e:
            raise LLMStoreError(f"stored response {path.name} is malformed ({e})") from None
        return rec

    def put(self, fingerprint: str, summary: dict[str, Any], response: LLMResponse) -> None:
        atomic_write_json(self.path_for(fingerprint), {
            "format": STORE_FORMAT,
            "fingerprint": fingerprint,
            "request": summary,
            "response": {
                "parsed": response.parsed,
                "text": response.text,
                "model": response.model,
                "stop_reason": response.stop_reason,
                "usage": response.usage.to_dict(),
                "request_id": response.request_id,
            },
        })


def usage_of(record: dict[str, Any]) -> Usage:
    return Usage.from_dict(record["response"].get("usage") or {})
