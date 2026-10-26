"""Fine sampling refinement of moment boundaries (DESIGN §7.2 step 5, Phase 7c).

After labeling, the best-scoring video moments get a second, smaller look at their cut points: the source is resampled at
`media.fine_sampling_fps` in a short window around the moment's start and another around its end (the windows are clipped to the
moment's own segment and the asset, and never overlap), and a second vision request names -- as frame *indices*, never seconds --
the first frame that belongs in the moment and the last one. The code converts indices to frame-rounded source seconds with the
same "territory" rule as `best_frames` (a frame stands for the stretch halfway to its neighbours), clamps the result to the
windows and to the asset, and keeps the labeled range whenever the reply is invalid, null or not usable.

The refined range replaces `source_in_seconds`/`source_out_seconds` of the moment (so selection and planning, which read those
fields and fingerprint them, use it without further changes); the labeled range and everything the model saw stay in the moment's
`refinement` record for audit. Original media is only ever read.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trip_vlog.analysis import frames as fr
from trip_vlog.analysis.labels import schema_hash, segment_fps
from trip_vlog.analysis.vision import RECOVERABLE, MAX_EARLY_BAD_REQUESTS, Item, VisionSettings, _long
from trip_vlog.config import Config
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import ImagePart, LLMBadRequestError, LLMConfigError, LLMRequest, TextPart, Usage
from trip_vlog.llm.untrusted import PREAMBLE
from trip_vlog.logging_utils import RunLogger, Status
from trip_vlog.planning.duration import frames_to_float, seconds_to_frames
from trip_vlog.prompts import PromptTemplate, load_template
from trip_vlog.state import stable_hash

REFINE_VERSION = 1            # bump when the refinement record, window rules or index-to-seconds conversion change
REFINE_ANALYZER = "vision_refine:"
FINE_REL = "media_index/analysis/fine"
FINE_SHEETS_REL = "media_index/analysis/fine_sheets"
MIN_MOMENT_SECONDS = 1.0      # shorter moments are not refined
MIN_REFINED_SECONDS = 0.5     # a refined range shorter than this is rejected
TOKENS_PER_ITEM = 100
TOKENS_BASE = 200
SIDES = ("start", "end")


# ---------------------------------------------------------------- schema
def refine_schema() -> dict[str, Any]:
    idx = {"type": ["integer", "null"], "minimum": 0}
    item = {
        "type": "object",
        "properties": {
            "moment_id": {"type": "string", "description": "Copied exactly from the moment header."},
            "start_frame_index": {**idx, "description": "Index within the START window of the first frame to keep; null = keep the current start."},
            "end_frame_index": {**idx, "description": "Index within the END window of the last frame to keep; null = keep the current end."},
        },
        "required": ["moment_id", "start_frame_index", "end_frame_index"],
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://trip-vlog.local/schemas/boundary_refine_response:{REFINE_VERSION}",
        "title": "boundary_refine_response",
        "description": "Refined cut points of video moments as frame indices into the windows shown. No timestamps.",
        "type": "object",
        "properties": {"moments": {"type": "array", "items": item}},
        "required": ["moments"],
        "additionalProperties": False,
    }


def write_schemas(directory: Any) -> list[Path]:
    from trip_vlog.state import atomic_write_json

    p = Path(directory) / "boundary_refine_response.schema.json"
    atomic_write_json(p, refine_schema())
    return [p]


# ---------------------------------------------------------------- windows (pure integer frame arithmetic)
@dataclass(frozen=True)
class Window:
    side: str          # "start" | "end"
    start: int         # first source frame (inclusive)
    end: int           # one past the last source frame

    def frames(self) -> int:
        return self.end - self.start


def plan_windows(in_f: int, out_f: int, seg_start: int, seg_end: int, total_frames: int, span_f: int) -> tuple[Window, Window] | None:
    """Windows around the start (`in_f`) and end (`out_f`) of a moment, each `span_f` frames either side, clipped to the segment
    and the asset, and split at the moment's midpoint so they never overlap. None when the moment has no usable room."""
    lo, hi = max(0, seg_start), min(seg_end, total_frames)
    if span_f < 1 or not (lo <= in_f < out_f <= hi):
        return None
    mid = (in_f + out_f) // 2
    sw = Window("start", max(lo, in_f - span_f), min(in_f + span_f, mid))
    ew = Window("end", max(out_f - span_f, mid), min(hi, out_f + span_f))
    if sw.frames() < 1 or ew.frames() < 1:
        return None
    return sw, ew


def start_boundary(sampled: Sequence[int], idx: int, w: Window) -> int:
    """First source frame of the territory of sample `idx` (halfway to the previous sample; the window start for the first)."""
    return w.start if idx == 0 else (sampled[idx - 1] + sampled[idx] + 1) // 2


def end_boundary(sampled: Sequence[int], idx: int, w: Window) -> int:
    """One past the territory of sample `idx` (halfway to the next sample; the window end for the last)."""
    return w.end if idx == len(sampled) - 1 else (sampled[idx] + sampled[idx + 1] + 1) // 2


def _index(v: Any, n: int, what: str) -> tuple[int | None, str | None]:
    if v is None:
        return None, None
    if isinstance(v, bool) or not isinstance(v, int):
        return None, f"{what} {v!r} is not an integer or null"
    if not 0 <= v < n:
        return None, f"{what} {v} is outside the {n} frame(s) shown (valid 0..{n - 1})"
    return v, None


def resolve_reply(entry: Mapping[str, Any], windows: tuple[Window, Window], sampled: tuple[Sequence[int], Sequence[int]], *,
                  in_f: int, out_f: int, total_frames: int, fps: Fraction) -> tuple[dict[str, Any] | None, str | None]:
    """Turn one reply entry into a refined frame range. Returns ({"in_frame", "out_frame", ...}, None) or (None, why-invalid).
    The result lies inside the two windows and the asset, is at least MIN_REFINED_SECONDS long, and `applied` is False when
    it equals the labeled range (null indices, indices that land on the current boundaries, or a result that would be too short:
    then `reason` says so). Only out-of-range or non-integer indices are errors."""
    sw, ew = windows
    a, err = _index(entry.get("start_frame_index"), len(sampled[0]), "start_frame_index")
    if err:
        return None, err
    b, err = _index(entry.get("end_frame_index"), len(sampled[1]), "end_frame_index")
    if err:
        return None, err
    new_in = in_f if a is None else start_boundary(sampled[0], a, sw)
    new_out = out_f if b is None else end_boundary(sampled[1], b, ew)
    new_in = min(max(new_in, sw.start), sw.end)      # defence in depth: never outside the window, never outside the asset
    new_out = min(max(new_out, ew.start), ew.end)
    new_in, new_out = max(0, new_in), min(total_frames, new_out)
    if new_out <= new_in or (new_out - new_in) / float(fps) < MIN_REFINED_SECONDS - 1e-9:
        # two individually valid indices that leave too little: a usable "no better boundary" answer, not an invalid reply
        return {"in_frame": in_f, "out_frame": out_f, "applied": False,
                "reason": f"the chosen boundaries would leave less than {MIN_REFINED_SECONDS:g} s; kept the labeled range"}, None
    return {"in_frame": new_in, "out_frame": new_out, "applied": (new_in, new_out) != (in_f, out_f)}, None


# ---------------------------------------------------------------- settings and identity
@dataclass(frozen=True)
class RefineSettings:
    enabled: bool
    sampling: fr.SamplingSettings
    window_seconds: float
    min_score: float
    max_per_asset: int
    max_moments: int
    input_mode: str
    batch_max_images: int
    max_items_per_request: int
    max_tokens_cap: int
    model: str
    provider_extra: Mapping[str, Any]
    endpoint: str
    is_local: bool
    supports_images: bool
    image_max_side: int
    image_quality: int
    analyst: PromptTemplate
    prompt: PromptTemplate

    @classmethod
    def from_config(cls, config: Config, v: VisionSettings) -> "RefineSettings":
        m = config.media
        cap = max(1, min(m.vision_max_frames_per_segment, m.vision_batch_max_images // 2))   # a moment's two windows travel together
        sampling = fr.SamplingSettings(fps=float(m.fine_sampling_fps), max_frames=cap, max_side=v.sampling.max_side, quality=v.sampling.quality)
        return cls(
            enabled=bool(m.fine_sampling_enabled), sampling=sampling, window_seconds=float(m.fine_sampling_window_seconds),
            min_score=float(m.fine_sampling_min_score), max_per_asset=int(m.fine_sampling_max_per_asset),
            max_moments=int(m.fine_sampling_max_moments), input_mode=m.vision_input, batch_max_images=m.vision_batch_max_images,
            max_items_per_request=max(1, (v.max_tokens_cap - TOKENS_BASE) // TOKENS_PER_ITEM), max_tokens_cap=v.max_tokens_cap,
            model=v.model, provider_extra=v.provider_extra, endpoint=v.endpoint, is_local=v.is_local, supports_images=v.supports_images,
            image_max_side=v.image_max_side, image_quality=v.image_quality, analyst=v.analyst, prompt=load_template("boundary_refine", 1))

    def prompt_info(self) -> dict[str, Any]:
        return {"id": self.prompt.id, "version": self.prompt.version}

    def system(self) -> str:
        return f"{self.analyst.text}\n\n{self.prompt.text}"

    def identity(self) -> dict[str, Any]:
        """Everything but the moment itself that changes what a refinement would be: prompts, schema, model, endpoint api/params,
        the fine sampling (fps, frame cap, image size, FRAMES_VERSION), the window span and what the model receives."""
        ident: dict[str, Any] = {
            "v": REFINE_VERSION, "analyst": [self.analyst.id, self.analyst.version, self.analyst.sha256],
            "task": [self.prompt.id, self.prompt.version, self.prompt.sha256], "schema": schema_hash(refine_schema()),
            "model": self.model, "provider": dict(self.provider_extra), "image": [self.image_max_side, self.image_quality],
            "sampling": self.sampling.key(), "span": self.window_seconds, "input": self.input_mode, "min_s": MIN_REFINED_SECONDS,
        }
        if self.input_mode == "contact_sheet":
            ident["sheet"] = [fr.SHEET_TILE_W, fr.SHEET_VERSION]
        return ident

    def stage_key(self) -> str | None:
        """Part of the stage input hash: None while disabled; selection settings are included because they decide which moments change."""
        if not self.enabled:
            return None
        return stable_hash({"identity": self.identity(), "min_score": self.min_score, "per_asset": self.max_per_asset, "max": self.max_moments})

    def item_key(self, orig_in: float, orig_out: float) -> str:
        """Cache key of one moment's refinement. The asset content_version is checked by the cache row itself; the segment is in
        the analyzer name; the labeled range is here because a different label range is a different question."""
        return stable_hash({"identity": self.identity(), "in": orig_in, "out": orig_out})


@dataclass
class RefineItem:
    item: Item
    orig_in: float
    orig_out: float
    key: str
    windows: tuple[Window, Window] | None = None
    records: tuple[list[dict[str, Any]], list[dict[str, Any]]] = field(default_factory=lambda: ([], []))
    sheets: tuple[str | None, str | None] = (None, None)
    total_frames: int = 0
    fps: Fraction = Fraction(30)
    result: dict[str, Any] | None = None
    error: str | None = None
    model: str = ""

    @property
    def mid(self) -> str:
        return "moment_" + self.item.sid[4:]

    @property
    def n_images(self) -> int:
        return 2 if self.item.sheet_mode else len(self.records[0]) + len(self.records[1])


def eligible(it: Item, orig_in: float | None, orig_out: float | None) -> bool:
    return (it.kind == "video" and it.label is not None and orig_in is not None and orig_out is not None
            and it.seg.get("start_frame") is not None and orig_out - orig_in >= MIN_MOMENT_SECONDS)


def select(items: Sequence[tuple[Item, float]], s: RefineSettings) -> list[Item]:
    """The moments to refine: interest >= min_score, best first, at most `max_per_asset` per asset and `max_moments` overall.
    Deterministic (ties by confidence, then moment ID), so a capped run always picks the same set. `items` are (item, interest)."""
    def order(p: tuple[Item, float]) -> tuple[float, float, str]:
        return (-p[1], -float((p[0].label or {}).get("confidence") or 0.0), p[0].sid)

    per_asset: dict[str, int] = {}
    chosen: list[tuple[Item, float]] = []
    for p in sorted((p for p in items if p[1] >= s.min_score), key=order):
        aid = p[0].asset["asset_id"]
        if per_asset.get(aid, 0) < s.max_per_asset:
            per_asset[aid] = per_asset.get(aid, 0) + 1
            chosen.append(p)
    return [p[0] for p in chosen[:s.max_moments]]


def make_refine_item(it: Item, s: RefineSettings, orig_in: float, orig_out: float) -> RefineItem:
    fps = segment_fps(it.seg)
    dur = it.asset["duration_seconds"]
    total = int(Fraction(str(dur)) * fps) if dur else int(it.seg["end_frame"])
    ri = RefineItem(it, orig_in, orig_out, s.item_key(orig_in, orig_out), total_frames=min(total, int(it.seg["end_frame"])), fps=fps)
    span = max(1, seconds_to_frames(s.window_seconds, fps, "round"))
    in_f, out_f = seconds_to_frames(orig_in, fps), seconds_to_frames(orig_out, fps)
    ri.windows = plan_windows(in_f, out_f, int(it.seg["start_frame"]), int(it.seg["end_frame"]), ri.total_frames, span)
    return ri


def _pseudo_segment(ri: RefineItem, w: Window) -> dict[str, Any]:
    """The window as a segment for the existing frame sampler/extractor: its own ID (so stale-file cleanup never touches the
    labeling frames) and the source's frame rate."""
    seg = ri.item.seg
    return {"asset_id": seg["asset_id"], "segment_id": f"{ri.mid}_{w.side}", "start_frame": w.start, "end_frame": w.end, "fps": seg.get("fps")}


def plan_frames(ri: RefineItem, s: RefineSettings) -> None:
    """Fill in the planned frame records (and sheet paths) of both windows. Pure: nothing is extracted."""
    assert ri.windows is not None
    cv = ri.item.asset["content_version"]
    recs, sheets = [], []
    for w in ri.windows:
        ps = _pseudo_segment(ri, w)
        r = fr.frame_records(ps, cv, s.sampling)
        for x in r:
            x["artifact"] = x["artifact"].replace(fr.FRAMES_REL, FINE_REL, 1)
        recs.append(r)
        sheets.append(fr.sheet_rel(ps, cv, s.sampling).replace(fr.SHEETS_REL, FINE_SHEETS_REL, 1) if s.input_mode == "contact_sheet" else None)
    ri.records, ri.sheets = (recs[0], recs[1]), (sheets[0], sheets[1])


def prepare(ri: RefineItem, s: RefineSettings, ffmpeg: Path | None, root: Path) -> str | None:
    """Extract the fine frames (and sheets) of both windows with the labeling code. Error string or None."""
    if ri.windows is None:
        return "no room to refine"
    for w, recs, sheet in zip(ri.windows, ri.records, ri.sheets):
        ps = _pseudo_segment(ri, w)
        err = fr.extract_segment_frames(ffmpeg, ri.item.src, ps, recs, root, s.sampling)
        if err:
            return f"fine frame extraction failed: {err}"
        if sheet is not None:
            serr = fr.make_segment_sheet(recs, ps, root, sheet)
            if serr:
                return serr
    return None


# ---------------------------------------------------------------- request
def _identity(s: RefineSettings, ri: RefineItem, what: str, ref: Any) -> str:
    return stable_hash({"what": f"fine_{what}", "cv": ri.item.asset["content_version"], "ref": ref, "sampling": s.sampling.key()})


def build_request(s: RefineSettings, batch: Sequence[RefineItem], root: Path) -> LLMRequest:
    parts: list[Any] = [TextPart(f"{PREAMBLE}\n\nRefine the cut points of the {len(batch)} video moment(s) below. IDs, in order: "
                                 f"{', '.join(r.mid for r in batch)}.")]
    for ri in batch:
        parts.append(TextPart(f"=== Moment {ri.mid} ===\nCurrent start t={ri.orig_in:.2f}s, current end t={ri.orig_out:.2f}s in the source clip. "
                              f"Two windows follow: START (around the current start) and END (around the current end)."))
        for w, recs, sheet in zip(ri.windows or (), ri.records, ri.sheets):
            t0, t1 = frames_to_float(w.start, ri.fps), frames_to_float(w.end, ri.fps)
            label = w.side.upper()
            if sheet is not None:
                parts.append(TextPart(f"{label} window of {ri.mid}, t={t0:.2f}s to {t1:.2f}s: one contact sheet with {len(recs)} frame(s) in time order; "
                                      f"each tile is labelled '#index t=seconds'. Use those indices."))
                parts.append(ImagePart(_long(root / sheet), identity=_identity(s, ri, "sheet", [w.side, fr.SHEET_TILE_W, fr.SHEET_VERSION, *(f["frame"] for f in recs)])))
            else:
                parts.append(TextPart(f"{label} window of {ri.mid}, t={t0:.2f}s to {t1:.2f}s: {len(recs)} frame(s) follow in time order."))
                for f in recs:
                    parts.append(TextPart(f"Moment {ri.mid} | {w.side} frame {f['index']} | t={f['timestamp_seconds']:.2f}s"))
                    parts.append(ImagePart(_long(root / f["artifact"]), identity=_identity(s, ri, "frame", [w.side, f["frame"]])))
    max_tokens = min(TOKENS_PER_ITEM * len(batch) + TOKENS_BASE, s.max_tokens_cap)
    return LLMRequest(s.prompt.id, str(s.prompt.version), s.system(), tuple(parts), refine_schema(), max_tokens)


def make_batches(items: Sequence[RefineItem], max_images: int, max_items: int) -> list[list[RefineItem]]:
    """Greedy, order-preserving; a moment's windows are never split, one that alone exceeds the limit gets its own request."""
    out: list[list[RefineItem]] = []
    cur: list[RefineItem] = []
    n = 0
    for ri in items:
        if cur and (n + ri.n_images > max_images or len(cur) >= max_items):
            out.append(cur)
            cur, n = [], 0
        cur.append(ri)
        n += ri.n_images
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- record (what is cached and stored on the moment)
def make_record(ri: RefineItem, s: RefineSettings, model: str) -> dict[str, Any]:
    assert ri.result is not None and ri.windows is not None
    r = ri.result
    rec = {
        "version": REFINE_VERSION, "applied": bool(r["applied"]),
        "original_in_seconds": ri.orig_in, "original_out_seconds": ri.orig_out,
        "refined_in_seconds": frames_to_float(r["in_frame"], ri.fps), "refined_out_seconds": frames_to_float(r["out_frame"], ri.fps),
        "start_frame_index": r["start_frame_index"], "end_frame_index": r["end_frame_index"], "reason": r.get("reason"),
        "fps": s.sampling.fps, "window_seconds": s.window_seconds,
        "windows": {w.side: {"start_seconds": frames_to_float(w.start, ri.fps), "end_seconds": frames_to_float(w.end, ri.fps),
                             "frames": list(recs)} for w, recs in zip(ri.windows, ri.records)},
        "model": model, "prompt": s.prompt_info(),
    }
    return rec


def apply_refinement(m: dict[str, Any], rec: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stored moment with the refinement applied: the refined range becomes source_in/out (what selection and planning read),
    the labeled range stays in `refinement.original_*`. A record that changed nothing is kept for audit only."""
    if not rec:
        return m
    m["refinement"] = dict(rec)
    if rec.get("applied"):
        m["source_in_seconds"] = rec["refined_in_seconds"]
        m["source_out_seconds"] = rec["refined_out_seconds"]
    return m


# ---------------------------------------------------------------- loop
@dataclass
class RefineStats:
    candidates: int = 0
    cached: int = 0
    requests: int = 0
    cache_hits: int = 0
    refined: int = 0
    unchanged: int = 0
    rejected: int = 0        # reply invalid or moment missing from it (response evicted from the cache)
    failed: int = 0          # frames could not be extracted / request failed
    skipped: dict[str, int] = field(default_factory=dict)   # reason -> moments selected but not refined
    usage: Usage = field(default_factory=Usage)
    errors: list[dict[str, str]] = field(default_factory=list)


class Refiner:
    """Runs refinement batches through `gateway`; calls `accept(ri)` for each validated result right away (the stage persists it).
    Failures never fall back to anything but the labeled range and are not cached, so the next run asks again."""

    def __init__(self, gateway: Gateway, settings: RefineSettings, root: Path, ffmpeg: Path | None, *, accept: Callable[[RefineItem], None],
                 logger: RunLogger | None = None, answered: int = 0, stats: RefineStats | None = None):
        self.gateway, self.settings, self.root, self.ffmpeg, self.accept, self.logger = gateway, settings, root, ffmpeg, accept, logger
        self.stats = stats if stats is not None else RefineStats()
        self._answered = answered   # requests the server accepted so far in this run (labeling included)
        self._bad_streak = 0

    def _error(self, ri: RefineItem, msg: str, phase: str) -> None:
        ri.error = msg
        if phase == "validate":
            self.stats.rejected += 1
        else:
            self.stats.failed += 1
        self.stats.errors.append({"segment_id": ri.item.sid, "asset_id": ri.item.asset["asset_id"], "path": ri.item.asset["relative_path"], "error": msg})
        if self.logger is not None:
            self.logger.event("analyze", Status.FAILED, asset_id=ri.item.asset["asset_id"], segment_id=ri.item.sid, phase=f"refine_{phase}", error=msg)

    def run(self, items: Sequence[RefineItem]) -> None:
        ready = []
        for ri in items:
            err = prepare(ri, self.settings, self.ffmpeg, self.root)
            if err:
                self._error(ri, err, "prepare")
            else:
                ready.append(ri)
        for batch in make_batches(ready, self.settings.batch_max_images, self.settings.max_items_per_request):
            self._batch(batch)

    def _batch(self, batch: list[RefineItem]) -> None:
        gw, s = self.gateway, self.settings
        req = build_request(s, batch, self.root)
        self.stats.requests += 1
        try:
            resp = gw.complete(req)
        except RECOVERABLE as e:
            self._bad_streak = self._bad_streak + 1 if isinstance(e, LLMBadRequestError) else 0
            if self._bad_streak >= MAX_EARLY_BAD_REQUESTS and not self._answered:
                raise LLMConfigError(f"stopping: the first {self._bad_streak} requests were rejected as invalid and none succeeded, so this is "
                                     f"probably an endpoint configuration problem (model, params, max_tokens_field, structured_output). "
                                     f"Server said: {str(e)[:300]}") from None
            for ri in batch:
                self._error(ri, f"{type(e).__name__}: {e}"[:300], "request")
            return
        self._bad_streak = 0
        self._answered += 0 if resp.cached else 1
        self.stats.usage = self.stats.usage + (resp.usage if not resp.cached else Usage())
        self.stats.cache_hits += int(resp.cached)
        by_id: dict[str, Mapping[str, Any]] = {}
        ids = {ri.mid for ri in batch}
        for e in resp.parsed["moments"]:
            if e.get("moment_id") in ids:
                by_id.setdefault(e["moment_id"], e)
        bad = 0
        for ri in batch:
            e = by_id.get(ri.mid)
            if e is None:
                self._error(ri, "moment missing from the reply", "validate")
            else:
                assert ri.windows is not None
                res, err = resolve_reply(e, ri.windows, ([f["frame"] for f in ri.records[0]], [f["frame"] for f in ri.records[1]]),
                                         in_f=seconds_to_frames(ri.orig_in, ri.fps), out_f=seconds_to_frames(ri.orig_out, ri.fps),
                                         total_frames=ri.total_frames, fps=ri.fps)
                if err or res is None:
                    self._error(ri, f"invalid refinement: {err}", "validate")
                else:
                    res.update(start_frame_index=e["start_frame_index"], end_frame_index=e["end_frame_index"])
                    ri.result, ri.model, ri.error = res, resp.model, None
                    self.stats.refined += int(res["applied"])
                    self.stats.unchanged += int(not res["applied"])
                    self.accept(ri)
                    continue
            bad += 1
        if bad and gw.cache is not None:
            gw.cache.discard(resp.fingerprint)   # schema-valid but unusable: a later run must ask again, not reuse it
        if self.logger is not None:
            self.logger.event("analyze", Status.SUCCEEDED, batch_size=len(batch), images=sum(r.n_images for r in batch),
                              refined=len(batch) - bad, cache="hit" if resp.cached else "miss", kind="refine")


if __name__ == "__main__":  # python -m trip_vlog.analysis.refine [dir]
    import sys

    for _p in write_schemas(sys.argv[1] if len(sys.argv) > 1 else "schemas"):
        print(_p)
