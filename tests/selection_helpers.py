"""Shared doubles for the LLM selection tests: one fake text endpoint that answers the story-outline requests AND the clip-selection
requests from the request text itself (no network, no ffmpeg), and a RunContext stand-in with a seeded catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from analysis_helpers import make_ctx
from outline_helpers import ID_RE, TARGET_RE, api_reply, default_draft, gateway_for, outline_config, seed, trip_items

from trip_vlog import outline_stages as os_
from trip_vlog.llm.transport import HttpResponse

SCENE_RE = re.compile(r"^### (scene_\d+) \| role: (\w+) \| budget: ([0-9.]+) s", re.M)


def planner_config(planning: dict | None = None, editorial: dict | None = None, project: dict | None = None, workflow: dict | None = None, **top: Any):
    return outline_config(planning, editorial, {"target_duration_seconds": 30, **(project or {})},
                          workflow={"planner": "llm", **(workflow or {})}, **top)


@dataclass
class PlanCall:
    texts: list[str]
    scenes: dict[str, dict[str, Any]]    # scene_id -> {"budget": float, "cands": [{"id", "use", "kind"}]}
    retry: bool
    body: dict[str, Any]


def _clip(mid: str, dur: float, **over: Any) -> dict[str, Any]:
    c = {"moment_id": mid, "target_duration_seconds": round(dur, 2), "anchor": "center", "track_role": "primary_video",
         "audio_policy": "natural_sound", "transition_in": {"type": "cut", "duration_seconds": 0.0}, "rationale": "A good shot.",
         "intentional_repeat_reason": None}
    c.update(over)
    return c


def default_plan(call: PlanCall) -> dict[str, Any]:
    """Per scene: up to ceil(budget / 4) candidates, durations spread to the budget without exceeding any moment's 'use'."""
    scenes = []
    for sid, info in call.scenes.items():
        cands = info["cands"]
        n = max(1, min(len(cands), -(-int(info["budget"]) // 4)))
        chosen = cands[:n]
        dur = [info["budget"] / n] * n
        caps = [(c["use"] if c["use"] is not None else 10.0) for c in chosen]
        dur = [min(d, c) for d, c in zip(dur, caps)]
        for _ in range(5):                                  # hand leftover time to clips that still have room
            left = info["budget"] - sum(dur)
            room = [i for i in range(n) if dur[i] < caps[i] - 1e-6]
            if left < 0.01 or not room:
                break
            for i in room:
                dur[i] = min(caps[i], dur[i] + left / len(room))
        scenes.append({"scene_id": sid, "clips": [_clip(c["id"], max(1.0, d)) for c, d in zip(chosen, dur)]})
    return {"scenes": scenes, "titles": [], "uncertainties": ["Check the order of the market clips."]}


@dataclass
class PlannerTransport:
    """Fake text endpoint. `respond_plan(call, default)` / `respond_outline(call, default)` return a dict (the reply) or an HttpResponse."""

    respond_plan: Callable[[PlanCall, dict[str, Any]], Any] | None = None
    respond_outline: Callable[[Any, dict[str, Any]], Any] | None = None
    plan_calls: list[PlanCall] = field(default_factory=list)
    outline_calls: list[Any] = field(default_factory=list)

    def send(self, request) -> HttpResponse:
        from outline_helpers import OutlineCall

        body = json.loads(request.body.decode("utf-8"))
        msgs = body["messages"]
        texts = [b["text"] for b in msgs[0]["content"] if b["type"] == "text"]
        joined = "\n".join(texts)
        retry = len(msgs) > 1 or any(t.startswith("Your previous reply was:") for t in texts)
        if body["system"].startswith("Convert the approved outline"):
            scenes: dict[str, dict[str, Any]] = {}
            parts = SCENE_RE.split(texts[0])
            for i in range(1, len(parts), 4):
                sid, budget, chunk = parts[i], float(parts[i + 2]), parts[i + 3]
                cands = []
                for line in chunk.splitlines():
                    if line.startswith('{"id":"moment_'):
                        h = json.loads(line)
                        cands.append({"id": h["id"], "use": h.get("use"), "kind": h["kind"]})
                scenes[sid] = {"budget": budget, "cands": cands}
            call = PlanCall(texts, scenes, retry, body)
            self.plan_calls.append(call)
            d = default_plan(call)
            out = self.respond_plan(call, d) if self.respond_plan else d
            return out if isinstance(out, HttpResponse) else api_reply(out)
        ids = list(dict.fromkeys(ID_RE.findall(texts[0])))
        m = TARGET_RE.search(joined)
        call = OutlineCall(texts, ids, float(m[1]) if m else 0.0, retry, body)
        self.outline_calls.append(call)
        d = default_draft(ids, call.target)
        out = self.respond_outline(call, d) if self.respond_outline else d
        return out if isinstance(out, HttpResponse) else api_reply(out)


def make_plan_ctx(tmp_path, cfg=None, items=None, transport: PlannerTransport | None = None):
    """RunContext stand-in with a seeded catalog; the outline stage module's gateway hook is the fake endpoint (it serves both stages)."""
    cfg = cfg or planner_config()
    ctx = make_ctx(tmp_path, cfg)
    ids = seed(ctx, items if items is not None else trip_items())
    tr = transport or PlannerTransport()
    os_.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    ctx.paths = SimpleNamespace(media_root=ctx.paths.media_root, workspace=ctx.workspace.root, outputs={})
    return ctx, tr, ids


def plan_runner(ctx):
    """analyze (stub) -> outline -> outline_approval -> plan -> approval, as the draft runner registers them."""
    from trip_vlog.draft_stages import approval_stage, plan_stage
    from trip_vlog.logging_utils import Status
    from trip_vlog.selection_stages import selection_inputs
    from trip_vlog.state import StageResult, StageRunner

    r = StageRunner(ctx.manifest, ctx.logger, ctx)
    r.register("analyze", lambda c: StageResult(Status.SUCCEEDED, {}), always_run=True)
    os_.register_outline_stages(r, after="analyze")
    r.register("plan", plan_stage, deps=("outline_approval",), inputs=selection_inputs,
               verify=lambda c: (c.workspace.root / "plans" / "edit_plan.json").is_file())
    r.register("approval", approval_stage, deps=("plan",), always_run=True)
    return r


__all__ = ["planner_config", "PlanCall", "PlannerTransport", "make_plan_ctx", "plan_runner", "default_plan", "_clip", "trip_items", "seed"]
