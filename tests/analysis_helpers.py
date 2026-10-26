"""Shared doubles for the vision-labeling tests: a fake HTTP transport that answers labeling requests from the request itself,
and builders for a synthetic catalog + RunContext (no ffmpeg or network)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from PIL import Image

from trip_vlog.config import parse_config
from trip_vlog.llm.factory import make_gateway
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.logging_utils import RunLogger
from trip_vlog.media.catalog import Catalog
from trip_vlog.media.ids import asset_id_for
from trip_vlog.media.segment import SegmentSettings, build_segments, moments_from_segments
from trip_vlog.state import Manifest, Workspace

SID_RE = re.compile(r"=== (?:Segment|Photo) (seg_[0-9a-f]{16}) ===")
FRAME_RE = re.compile(r"Segment (seg_[0-9a-f]{16}) \| frame (\d+) \| t=([0-9.]+)s")
ENV = {"ANTHROPIC_API_KEY": "sk-ant-test-key-1", "OPENAI_API_KEY": "sk-openai-test-key-2"}


def reply(entries: list[dict[str, Any]], *, stop="end_turn", model="test-model-resolved") -> HttpResponse:
    body = {"id": "msg_1", "type": "message", "model": model, "stop_reason": stop,
            "content": [{"type": "text", "text": json.dumps({"segments": entries})}],
            "usage": {"input_tokens": 100, "output_tokens": 50}}
    return HttpResponse(200, {"request-id": "req_x"}, json.dumps(body).encode("utf-8"))


def entry(sid: str, n_frames: int | None, **over: Any) -> dict[str, Any]:
    """A valid label entry; deterministic from the segment id."""
    h = int(hashlib.sha256(sid.encode()).hexdigest()[:4], 16)
    e: dict[str, Any] = {
        "segment_id": sid, "description": f"A test scene {sid[-6:]}", "visible_entities": ["sea", "boat"], "event": None,
        "editorial_tags": ["scenic"], "quality": {"sharpness": "good", "stability": "ok", "exposure": "ok"},
        "interest": round((h % 100) / 100, 2), "confidence": 0.8, "uncertain": False, "uncertainty_note": None,
    }
    if n_frames is not None:
        e["best_frames"] = None
    e.update(over)
    return e


@dataclass
class Call:
    sids: list[str]
    frames: dict[str, list[int]]
    images: int
    texts: list[str]
    body: dict[str, Any]
    retry: bool


MID_RE = re.compile(r"=== Moment (moment_[0-9a-f]{16}) ===")
FINE_RE = re.compile(r"Moment (moment_[0-9a-f]{16}) \| (start|end) frame (\d+) \| t=([0-9.]+)s")


def refine_reply(entries: list[dict[str, Any]], *, stop="end_turn") -> HttpResponse:
    body = {"id": "msg_2", "type": "message", "model": "test-model-resolved", "stop_reason": stop,
            "content": [{"type": "text", "text": json.dumps({"moments": entries})}],
            "usage": {"input_tokens": 60, "output_tokens": 20}}
    return HttpResponse(200, {"request-id": "req_r"}, json.dumps(body).encode("utf-8"))


@dataclass
class RefineCall:
    mids: list[str]
    frames: dict[str, dict[str, list[tuple[int, float]]]]    # moment -> side -> [(index, t)]
    images: int
    texts: list[str]
    body: dict[str, Any]


@dataclass
class LabelingTransport:
    """Answers every labeling request. `respond(call, default_entries)` may return entries (list) or an HttpResponse."""

    respond: Callable[[Call, list[dict[str, Any]]], Any] | None = None
    best: Callable[[str, int], dict | None] | None = None
    calls: list[Call] = field(default_factory=list)
    refine: Callable[[RefineCall, list[dict[str, Any]]], Any] | None = None     # fine sampling requests (boundary refinement)
    refine_calls: list[RefineCall] = field(default_factory=list)

    def _send_refine(self, body, blocks, texts) -> HttpResponse:
        mids = [m for t in texts for m in MID_RE.findall(t)]
        frames: dict[str, dict[str, list[tuple[int, float]]]] = {}
        for t in texts:
            for mid, side, idx, ts in FINE_RE.findall(t):
                frames.setdefault(mid, {}).setdefault(side, []).append((int(idx), float(ts)))
        call = RefineCall(mids, frames, sum(1 for b in blocks if b["type"] == "image"), texts, body)
        self.refine_calls.append(call)
        default = [{"moment_id": m, "start_frame_index": None, "end_frame_index": None} for m in mids]
        out = self.refine(call, default) if self.refine else default
        return out if isinstance(out, HttpResponse) else refine_reply(out)

    def send(self, request) -> HttpResponse:
        body = json.loads(request.body.decode("utf-8"))
        blocks = body["messages"][0]["content"]
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        if "Refine the cut points" in texts[0]:
            return self._send_refine(body, blocks, texts)
        sids = [m for t in texts for m in SID_RE.findall(t)]
        frames: dict[str, list[int]] = {}
        for t in texts:
            for sid, idx, _ in FRAME_RE.findall(t):
                frames.setdefault(sid, []).append(int(idx))
        call = Call(sids, frames, sum(1 for b in blocks if b["type"] == "image"), texts, body, any(t.startswith("Correction:") for t in texts))
        self.calls.append(call)
        is_photo_request = "photo(s)" in texts[0]
        default = []
        for sid in sids:
            n = None if is_photo_request else len(frames.get(sid, [])) or 1
            e = entry(sid, n)
            if n is not None and self.best is not None:
                e["best_frames"] = self.best(sid, n)
            default.append(e)
        out = self.respond(call, default) if self.respond else default
        return out if isinstance(out, HttpResponse) else reply(out)


def labeling_config(**media: Any):
    return parse_config({"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}}, "media": media})


def gateway_for(cfg, ws_root: Path, transport, **kw):
    return make_gateway(cfg, ws_root, "vision", transport=transport, env=ENV, sleep=lambda s: None, mode="live", **kw)


# ---------------------------------------------------------------- synthetic catalog (no ffmpeg)
F30 = Fraction(30)


def add_video(cat: Catalog, rel: str, duration: float, *, cv: str | None = None, window: float = 6.0, gps: dict | None = None,
              w: int = 1920, h: int = 1080) -> tuple[str, list[dict]]:
    aid = asset_id_for(rel)
    cv = cv or f"fast:{rel}"
    cat.upsert_asset({"asset_id": aid, "relative_path": rel, "extension": ".mp4", "size_bytes": 1, "mtime_ns": 1, "content_version": cv,
                      "sha256": None, "media_type": "video", "duration_seconds": duration, "width": w, "height": h, "display_width": w,
                      "display_height": h, "frame_rate_num": 30, "frame_rate_den": 1, "is_vfr": 0, "rotation_degrees": 0, "codec": "h264",
                      "pix_fmt": "yuv420p", "hdr": None, "audio_streams": 0, "capture_time": None, "capture_time_source": None,
                      "gps_json": json.dumps(gps) if gps else None, "format_name": "mov", "technical_notes_json": "[]", "probe_status": "ok",
                      "probe_error": None})
    segs = build_segments(aid, "video", duration, F30, {"cuts": [], "silences": [], "windows": [], "has_audio": False},
                          SegmentSettings(window_seconds=window))
    cat.replace_segments(aid, cv, segs)
    cat.replace_moments(aid, cv, moments_from_segments(segs, cv))
    return aid, segs


def add_photo(cat: Catalog, rel: str, *, cv: str | None = None, gps: dict | None = None, w: int = 64, h: int = 48) -> tuple[str, list[dict]]:
    aid = asset_id_for(rel)
    cv = cv or f"fast:{rel}"
    cat.upsert_asset({"asset_id": aid, "relative_path": rel, "extension": ".jpg", "size_bytes": 1, "mtime_ns": 1, "content_version": cv,
                      "sha256": None, "media_type": "image", "duration_seconds": None, "width": w, "height": h, "display_width": w,
                      "display_height": h, "frame_rate_num": None, "frame_rate_den": None, "is_vfr": 0, "rotation_degrees": 0,
                      "codec": "mjpeg", "pix_fmt": None, "hdr": None, "audio_streams": 0, "capture_time": None, "capture_time_source": None,
                      "gps_json": json.dumps(gps) if gps else None, "format_name": "image2", "technical_notes_json": "[]",
                      "probe_status": "ok", "probe_error": None})
    segs = build_segments(aid, "image", None, None, None, SegmentSettings())
    cat.replace_segments(aid, cv, segs)
    cat.replace_moments(aid, cv, moments_from_segments(segs, cv))
    return aid, segs


def write_photo(path: Path, color=(200, 30, 30), size=(64, 48), pattern: int = 0) -> Path:
    """A small JPEG; `pattern` varies the structure (not only the colour) so dHashes differ between patterns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    px = im.load()
    for x in range(size[0]):
        for y in range(size[1]):
            v = ((x * (pattern + 1) * 7 + y * (3 + pattern)) % 255)
            px[x, y] = (v, (v * 2) % 255, (255 - v))
    im.save(path, format="JPEG")
    return path


def fake_frames(root: Path, segs: list[dict], aid: str, cv: str, settings) -> None:
    """Create the planned frame files for every video segment (so no ffmpeg is needed)."""
    from trip_vlog.analysis import frames as fr

    for s in segs:
        for r in fr.frame_records(s, cv, settings):
            p = root / r["artifact"]
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 54), (r["index"] * 20 % 255, 80, 120)).save(p, format="JPEG")


def make_ctx(tmp_path: Path, cfg=None, *, ffmpeg: Path | None = None) -> SimpleNamespace:
    """RunContext stand-in with a real Catalog, Workspace and logger."""
    cfg = cfg or labeling_config()
    ws = Workspace(tmp_path / "ws")
    ws.ensure_dirs()
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    cat = Catalog(ws.catalog_path)
    logger = RunLogger(ws.run_log, ws.errors_log, "run-test")
    return SimpleNamespace(config=cfg, workspace=ws, catalog=cat, logger=logger, ffmpeg=ffmpeg, ffprobe=None,
                           paths=SimpleNamespace(media_root=media, workspace=ws.root, outputs={}), manifest=Manifest(ws.manifest_path),
                           data={}, echo=lambda *_: None)
