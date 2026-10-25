"""Structured JSONL logging with secret masking (DESIGN §17, §18)."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


MASK = "***"
_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?)[^\s,;\"']+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential)s?\s*[:=]\s*[\'\"]?)[^\s,;\"']+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "***"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def secret_env_values(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return sorted(
        {v for k, v in env.items() if _SECRET_ENV_NAME.search(k) and v and len(v) >= 6},
        key=len,
        reverse=True,
    )


def sanitize(text: object, *, env: Mapping[str, str] | None = None, redactions: Iterable[tuple[str, str]] = ()) -> str:
    """Mask secrets (env-var values with secret-like names, key=value pairs, bearer tokens, sk- keys).

    `redactions` is a list of (literal, replacement) pairs applied first (e.g. media root -> '<media_root>').
    """
    s = str(text)
    for literal, repl in sorted(redactions, key=lambda p: len(p[0]), reverse=True):
        if literal:
            s = s.replace(literal, repl)
    for value in secret_env_values(env):
        s = s.replace(value, MASK)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


class RunLogger:
    """Append-only JSONL logs: run.jsonl (everything) and errors.jsonl (failures only)."""

    def __init__(self, run_log: Path, errors_log: Path, run_id: str, redactions: Iterable[tuple[str, str]] = ()):
        self.run_log = Path(run_log)
        self.errors_log = Path(errors_log)
        self.run_id = run_id
        self.redactions = list(redactions)
        self._lock = threading.Lock()
        self.run_log.parent.mkdir(parents=True, exist_ok=True)
        self.errors_log.touch(exist_ok=True)

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock, open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def clean(self, text: object) -> str:
        return sanitize(text, redactions=self.redactions)

    def event(
        self,
        stage: str,
        status: Status | str,
        *,
        asset_id: str | None = None,
        cache: str | None = None,  # "hit" | "miss"
        duration_ms: float | None = None,
        error: object = None,
        **fields: Any,
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {"ts": utc_now_iso(), "run_id": self.run_id, "stage": stage, "status": str(status)}
        if asset_id is not None:
            rec["asset_id"] = asset_id
        if cache is not None:
            rec["cache"] = cache
        if duration_ms is not None:
            rec["duration_ms"] = round(duration_ms, 2)
        if error is not None:
            rec["error"] = self.clean(error)
        for k, v in fields.items():
            if v is not None:
                rec[k] = self.clean(v) if isinstance(v, str) else v
        self._append(self.run_log, rec)
        if error is not None or str(status) == Status.FAILED:
            self._append(self.errors_log, rec)
        return rec


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
