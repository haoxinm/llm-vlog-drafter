"""Doubles for the revision tests: a fake revision endpoint (Anthropic-shaped HTTP) that reads ids and the request out of the request text, reply
builders and a config builder. No network, no ffmpeg."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from analysis_helpers import ENV

from trip_vlog.config import parse_config
from trip_vlog.llm.factory import make_gateway
from trip_vlog.llm.transport import HttpResponse

CLIP_RE = re.compile(r"^([A-Za-z0-9_.-]+) \| (video|photo|image|audio) \| (moment_[0-9a-f]+)", re.M)
SCENE_RE = re.compile(r"^### (scene_\d+) \|", re.M)
ALT_RE = re.compile(r"^(moment_[0-9a-f]+) \| (?:photo|video, usable [0-9.]+ s) \|", re.M)
REQ_RE = re.compile(r"BEGIN REQUEST\n(.*?)\nEND REQUEST", re.S)


def revise_config(revise: dict | None = None, workflow: dict | None = None, project: dict | None = None, llm: dict | None = None,
                  editorial: dict | None = None, **top: Any):
    raw: dict[str, Any] = {"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}, **(llm or {})},
                           "revise": {"confirm_duration_change_min_seconds": 1.0, **(revise or {})}, "project": {"target_duration_seconds": 18.5, **(project or {})}}
    if workflow:
        raw["workflow"] = workflow
    if editorial:
        raw["editorial"] = editorial
    raw.update(top)
    return parse_config(raw)


def gateway_for(cfg, ws_root: Path, transport, **kw):
    return make_gateway(cfg, ws_root, "revise", transport=transport, env=ENV, sleep=lambda s: None, mode="live", **kw)


def reply(obj: Any, *, stop: str = "end_turn", model: str = "test-model-resolved") -> HttpResponse:
    body = {"id": "msg_1", "type": "message", "model": model, "stop_reason": stop, "content": [{"type": "text", "text": json.dumps(obj)}],
            "usage": {"input_tokens": 1500, "output_tokens": 300}}
    return HttpResponse(200, {"request-id": "req_r"}, json.dumps(body).encode("utf-8"))


def op(operation: str, why: str = "As asked.", part: str = "the request", **fields: Any) -> dict[str, Any]:
    o: dict[str, Any] = {"operation": operation, "clip_instance_id": None, "scene_id": None, "duration_seconds": None, "anchor": None, "moment_id": None,
                         "to_position": None, "transition_type": None, "transition_seconds": None, "audio": None, "shorten_by_seconds": None,
                         "rationale": why, "request_part": part}
    o.update(fields)
    return o


def answer(ops: list[dict[str, Any]] | None = None, *, ambiguous: list[dict[str, Any]] | None = None, destructive: bool = False, why: str | None = None,
           unsupported: list[dict[str, Any]] | None = None, summary: str = "Understood.") -> dict[str, Any]:
    return {"summary": summary, "operations": ops or [], "ambiguous": ambiguous or [], "destructive": destructive,
            "destructive_reason": why if destructive else None, "unsupported": unsupported or []}


def question(q: str = "Which beach?", about: str = "the beach", assumption: str | None = None) -> dict[str, Any]:
    return {"question": q, "about": about, "assumption": assumption}


@dataclass
class ReviseCall:
    texts: list[str]
    request: str
    clips: list[str]
    scenes: list[str]
    alternatives: list[str]
    body: dict[str, Any]


@dataclass
class ReviseTransport:
    """Fake revision endpoint. `respond(call)` returns the reply object (a dict) or an HttpResponse; default: no operations and a question."""

    respond: Callable[[ReviseCall], Any] | None = None
    calls: list[ReviseCall] = field(default_factory=list)

    def send(self, request) -> HttpResponse:
        body = json.loads(request.body.decode("utf-8"))
        blocks = body["messages"][0]["content"]
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        joined = "\n".join(texts)
        m = REQ_RE.search(joined)
        call = ReviseCall(texts, m[1] if m else "", list(dict.fromkeys(x[0] for x in CLIP_RE.findall(joined))), list(dict.fromkeys(SCENE_RE.findall(joined))),
                          list(dict.fromkeys(ALT_RE.findall(joined))), body)
        self.calls.append(call)
        out = self.respond(call) if self.respond is not None else answer(ambiguous=[question()])
        return out if isinstance(out, HttpResponse) else reply(out)
