"""Shared doubles for the story-outline tests: a seeded catalog of labeled moments, a fake HTTP transport that answers outline
requests from the request itself, and gateway/config builders. No network, no ffmpeg."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from analysis_helpers import ENV, add_photo, add_video, make_ctx

from trip_vlog.config import parse_config
from trip_vlog.llm.factory import make_gateway
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.planning.outline import ROLES, document_from_response

ID_RE = re.compile(r'"id":"(moment_[0-9a-f]+)"')
TARGET_RE = re.compile(r"vlog of about ([0-9.]+) seconds")


def outline_config(planning: dict | None = None, editorial: dict | None = None, project: dict | None = None, **top: Any):
    raw: dict[str, Any] = {"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}}}
    if planning:
        raw["planning"] = planning
    if editorial:
        raw["editorial"] = editorial
    raw["project"] = {"target_duration_seconds": 60, **(project or {})}
    raw.update(top)
    return parse_config(raw)


def gateway_for(cfg, ws_root: Path, transport, **kw):
    return make_gateway(cfg, ws_root, "text", transport=transport, env=ENV, sleep=lambda s: None, mode="live", **kw)


def _scene(role: str, title: str, ids: list[str], dur: float, note: str | None = None) -> dict[str, Any]:
    return {"role": role, "title": title, "purpose": f"Purpose of {title}", "tone": "warm", "target_duration_seconds": dur,
            "candidate_moment_ids": ids, "chronology_note": note}


def draft(scenes: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    d = {"premise": "A short test trip.", "story_mode_used": "mostly_chronological", "scenes": scenes,
         "hook": {"used": False, "moment_id": None, "justification": None}, "missing_coverage": [], "uncertainties": ["Confirm the place names."]}
    d.update(over)
    return d


def default_draft(ids: list[str], target: float, n_scenes: int = 3) -> dict[str, Any]:
    """Chronological scenes of consecutive ids whose budgets add up to the target."""
    n = max(1, min(n_scenes, len(ids)))
    size = -(-len(ids) // n)
    chunks = [ids[i:i + size] for i in range(0, len(ids), size)]
    roles = ["arrival_context", "activity_action", "scenic_breathing_room", "reflection_ending"]
    each = round(target / len(chunks), 2)
    scenes = [_scene(roles[min(i, len(roles) - 1)], f"Scene {i + 1}", c, each) for i, c in enumerate(chunks)]
    return draft(scenes)


def api_reply(obj: Any, *, stop: str = "end_turn", model: str = "test-model-resolved") -> HttpResponse:
    body = {"id": "msg_1", "type": "message", "model": model, "stop_reason": stop, "content": [{"type": "text", "text": json.dumps(obj)}],
            "usage": {"input_tokens": 1000, "output_tokens": 300}}
    return HttpResponse(200, {"request-id": "req_o"}, json.dumps(body).encode("utf-8"))


@dataclass
class OutlineCall:
    texts: list[str]
    ids: list[str]
    target: float
    retry: bool
    body: dict[str, Any]


@dataclass
class OutlineTransport:
    """Answers outline requests. `respond(call, default_draft)` may return a dict (the reply object) or an HttpResponse."""

    respond: Callable[[OutlineCall, dict[str, Any]], Any] | None = None
    calls: list[OutlineCall] = field(default_factory=list)

    def send(self, request) -> HttpResponse:
        body = json.loads(request.body.decode("utf-8"))
        msgs = body["messages"]
        texts = [b["text"] for b in msgs[0]["content"] if b["type"] == "text"]
        joined = "\n".join(texts)
        ids = list(dict.fromkeys(ID_RE.findall(texts[0])))
        m = TARGET_RE.search(joined)
        call = OutlineCall(texts, ids, float(m[1]) if m else 0.0, len(msgs) > 1 or any(t.startswith("Your previous reply was:") for t in texts), body)
        self.calls.append(call)
        d = default_draft(ids, call.target)
        out = self.respond(call, d) if self.respond else d
        return out if isinstance(out, HttpResponse) else api_reply(out)


# ---- seeded catalog ---------------------------------------------------------------------------------------
def seed(ctx, items: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Add assets with labeled moments. Item keys: rel, kind (video|photo), dur, capture, desc, interest, dup, speech, tags, audio,
    uncertain, labeled, w, h, gps. Returns rel -> [moment_id, ...] in segment order."""
    out: dict[str, list[str]] = {}
    for it in items:
        rel, kind = it["rel"], it.get("kind", "video")
        gps = it.get("gps")
        if kind == "video":
            aid, segs = add_video(ctx.catalog, rel, it.get("dur", 12.0), gps=gps, w=it.get("w", 1920), h=it.get("h", 1080))
        else:
            aid, segs = add_photo(ctx.catalog, rel, gps=gps, w=it.get("w", 64), h=it.get("h", 48))
        cv = f"fast:{rel}"
        if it.get("capture"):
            with ctx.catalog.conn:
                ctx.catalog.conn.execute("UPDATE assets SET capture_time=? WHERE asset_id=?", (it["capture"], aid))
        tx = []
        if it.get("speech"):
            tx = [{"transcript_id": f"tr_{aid[-8:]}", "asset_id": aid, "start_seconds": 0.0, "end_seconds": 5.0, "text": it["speech"],
                   "confidence": 0.9, "kind": "verbatim"}]
            ctx.catalog.replace_transcripts(aid, cv, tx)
        moments = []
        for i, row in enumerate(ctx.catalog.list_moments(aid)):
            m = json.loads(row["data_json"])
            labeled = it.get("labeled", True)
            if labeled:
                m.update({"description": f"{it.get('desc', rel)} #{i}", "editorial_tags": it.get("tags", ["scenic"]),
                          "quality": {"sharpness": "good", "stability": "ok", "exposure": "ok"},
                          "interest": round(min(1.0, it.get("interest", 0.5) + 0.01 * i), 2), "confidence": 0.8,
                          "uncertain": bool(it.get("uncertain")), "labeled": True, "source": "llm"})
            if it.get("audio"):
                m["audio_tags"] = it["audio"]
            if it.get("dup"):
                m["duplicate_group"] = it["dup"]
            if tx and i == 0:
                m["transcript_refs"] = [tx[0]["transcript_id"]]
            moments.append(m)
        ctx.catalog.upsert_moments(aid, cv, moments)
        out[rel] = [m["moment_id"] for m in moments]
    return out


def trip_items() -> list[dict[str, Any]]:
    """Two days: a morning video, a speaking clip, a market photo pair (near-duplicates), and a day-2 hike video."""
    return [
        {"rel": "day1/arrival.mp4", "dur": 12.0, "capture": "2026-10-01T09:00:00", "desc": "Walking out of the station", "interest": 0.4},
        {"rel": "day1/market.mp4", "dur": 8.0, "capture": "2026-10-01T11:30:00", "desc": "Market stalls", "interest": 0.7, "tags": ["food"],
         "speech": "Look at all this fruit, it is amazing"},
        {"rel": "day1/p1.jpg", "kind": "photo", "capture": "2026-10-01T11:40:00", "desc": "Fruit stall", "interest": 0.6, "dup": "dup_aaaa"},
        {"rel": "day1/p2.jpg", "kind": "photo", "capture": "2026-10-01T11:41:00", "desc": "Fruit stall again", "interest": 0.8, "dup": "dup_aaaa"},
        {"rel": "day2/hike.mp4", "dur": 18.0, "capture": "2026-10-02T08:15:00", "desc": "Trail above the valley", "interest": 0.9,
         "audio": ["wind_likely"], "w": 1080, "h": 1920},
    ]


def make_outline_ctx(tmp_path, cfg=None, items=None, transport: OutlineTransport | None = None):
    """RunContext stand-in with a seeded catalog; installs a fake text gateway on the outline stage module."""
    from trip_vlog import outline_stages as os_

    cfg = cfg or outline_config()
    ctx = make_ctx(tmp_path, cfg)
    ids = seed(ctx, items if items is not None else trip_items())
    tr = transport or OutlineTransport()
    os_.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    ctx.paths = SimpleNamespace(media_root=ctx.paths.media_root, workspace=ctx.workspace.root, outputs={})
    return ctx, tr, ids


__all__ = ["ROLES", "document_from_response", "outline_config", "gateway_for", "draft", "default_draft", "api_reply", "OutlineTransport",
           "OutlineCall", "seed", "trip_items", "make_outline_ctx", "ENV"]
