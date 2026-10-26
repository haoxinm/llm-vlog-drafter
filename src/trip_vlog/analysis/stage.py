"""The `analyze` stage: frame sampling -> vision labeling -> candidate moments (DESIGN §7.2, §7.4, §7.5).

Persistence and resumability
  * Every accepted label is written to the catalog's analysis cache (one row per segment, keyed by the prompt/schema/model/
    sampling identity) and its moment is upserted right away, batch by batch. A crashed or interrupted run therefore resumes
    at the first unlabeled batch; a finished run is a no-op (no frames extracted, no request sent).
  * Moments keep the baseline `moment_id` of their segment. Segments that could not be labeled keep the baseline moment,
    flagged `labeled: false`, and are retried by the next run.
  * `media_index/moments.jsonl` is re-exported at the end (it never contains GPS).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from trip_vlog.analysis import frames as fr
from trip_vlog.analysis import photos as ph
from trip_vlog.analysis import refine as rf
from trip_vlog.analysis.labels import labeled_moment, unlabeled_moment
from trip_vlog.analysis.vision import Item, Labeler, VisionSettings, make_batches
from trip_vlog.llm.factory import make_gateway
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMConfigError
from trip_vlog.logging_utils import Status
from trip_vlog.media.segment import moments_from_segments
from trip_vlog.speech import context as sc
from trip_vlog.state import StageResult, stable_hash

# Test hook: replace to inject a gateway built on a fake transport (`lambda ctx: make_gateway(..., transport=fake)`).
gateway_factory: Callable[[Any], Gateway] | None = None

ANALYZER = "vision_label:"
PHASH_ANALYZER = "photo_dhash"
PHASH_KEY = f"dhash64-v{ph.PHASH_VERSION}"
MAX_ERRORS_IN_SUMMARY = 10


class Work:
    def __init__(self, settings: VisionSettings, videos: list[Item], photos: list[Item], assets: list[Any]):
        self.settings, self.videos, self.photos, self.assets = settings, videos, photos, assets

    @property
    def items(self) -> list[Item]:
        return [*self.videos, *self.photos]

    def todo(self, kind: str) -> list[Item]:
        return [i for i in (self.videos if kind == "video" else self.photos) if not i.cached]


def _eligible(cat: Any, limit: int | None) -> list[Any]:
    rows = [r for r in cat.list_assets() if r["probe_status"] == "ok" and r["media_type"] in ("video", "image")]
    return rows[:limit] if limit else rows


def build_work(ctx: Any, limit: int | None = None) -> Work:
    """Plan the analysis: which segments/photos exist, which are already labeled (cache hit) and which frames would be sent."""
    cat = ctx.catalog
    settings = VisionSettings.from_config(ctx.config)
    rows = _eligible(cat, limit)
    videos: list[Item] = []
    photos: list[Item] = []
    for row in rows:
        aid, cv, mtype = row["asset_id"], row["content_version"], row["media_type"]
        kind = "video" if mtype == "video" else "photo"
        segs = [json.loads(s["data_json"]) for s in cat.list_segments(aid) if s["content_version"] == cv]
        base = {m["segment_id"]: m for m in moments_from_segments(segs, cv)}
        key = settings.cache_key(kind)
        tx_rows = sc.rows_for(cat, aid, cv) if (kind == "video" and ctx.config.speech.enabled) else []
        tx_rows = list(sc.usable_rows(tx_rows, len(segs)))   # a whole-clip (coarse) transcript is not spread over several segments
        src = ctx.paths.media_root / Path(*row["relative_path"].split("/"))
        for seg in segs:
            if kind == "video" and seg.get("start_frame") is None:
                continue
            text = sc.text_for(tx_rows, seg["start_seconds"], seg["end_seconds"]) if settings.transcript_max_chars else ""
            it = Item(kind, row, seg, base[seg["segment_id"]], src, settings.item_key(key, text),
                      sheet_mode=kind == "video" and settings.input_mode == "contact_sheet", transcript=text, tx_rows=tx_rows)
            rec = cat.cache_get(aid, ANALYZER + seg["segment_id"], it.key, cv)
            if rec is not None:
                it.label, it.frames, it.sheet, it.model, it.cached = rec["label"], rec.get("frames", []), rec.get("contact_sheet"), rec.get("model", ""), True
            elif kind == "video":
                it.frames = fr.frame_records(seg, cv, settings.sampling)
                it.sheet = fr.sheet_rel(seg, cv, settings.sampling)
            (videos if kind == "video" else photos).append(it)
    return Work(settings, videos, photos, rows)


def dry_run_report(ctx: Any, limit: int | None = None) -> dict[str, Any]:
    """What a real run would send, computed without extracting frames or contacting the endpoint."""
    w = build_work(ctx, limit)
    s = w.settings
    tv, tp = w.todo("video"), w.todo("photo")
    batches_v = make_batches(tv, s.batch_max_images, s.max_items_per_request)
    batches_p = make_batches(tp, s.batch_max_images, s.max_items_per_request)
    return {
        "endpoint": s.endpoint, "where": "local" if s.is_local else "remote", "model": s.model or None, "supports_images": s.supports_images,
        "assets": len(w.assets), "limit": limit,
        "video_segments": len(w.videos), "segments_with_transcript": sum(1 for i in w.videos if i.transcript), "video_segments_cached": len(w.videos) - len(tv), "video_segments_to_label": len(tv),
        "photos": len(w.photos), "photos_cached": len(w.photos) - len(tp), "photos_to_label": len(tp),
        "frames": sum(len(i.frames) for i in tv), "input": s.input_mode,
        "images": sum(i.n_images for i in [*tv, *tp]), "requests": len(batches_v) + len(batches_p),
        "batch_max_images": s.batch_max_images, "sampling_fps": s.sampling.fps, "max_frames_per_segment": s.sampling.max_frames,
        "fine_sampling": _fine_plan(ctx.config, s),
    }


def _fine_plan(cfg: Any, s: VisionSettings) -> dict[str, Any] | None:
    rs = rf.RefineSettings.from_config(cfg, s)
    if not rs.enabled:
        return None
    return {"fps": rs.sampling.fps, "window_seconds": rs.window_seconds, "min_score": rs.min_score, "max_per_asset": rs.max_per_asset,
            "max_moments": rs.max_moments}


def format_dry_run(rep: dict[str, Any]) -> str:
    lines = [
        f"analyze dry run (nothing is sent): endpoint '{rep['endpoint']}' ({rep['where']}), model {rep['model'] or '(not configured)'}",
        f"  assets considered     : {rep['assets']}" + (f" (--limit {rep['limit']})" if rep["limit"] else ""),
        f"  video segments        : {rep['video_segments']} ({rep['video_segments_cached']} already labeled, {rep['video_segments_to_label']} to label)",
        f"  photos                : {rep['photos']} ({rep['photos_cached']} already labeled, {rep['photos_to_label']} to label)",
        f"  with transcript text  : {rep['segments_with_transcript']} video segment(s) (spoken words are sent as untrusted context)",
        f"  frames to sample      : {rep['frames']} at {rep['sampling_fps']:g} fps, at most {rep['max_frames_per_segment']} per segment",
        f"  images to send        : {rep['images']} ({rep['input']} for videos, one image per photo)",
        f"  requests              : {rep['requests']} (up to {rep['batch_max_images']} images each)",
    ]
    fine = rep.get("fine_sampling")
    if fine:
        lines.append(f"  fine sampling         : after labeling, moments with interest >= {fine['min_score']:g} (at most {fine['max_per_asset']} per asset, "
                     f"{fine['max_moments']} per run) are resampled at {fine['fps']:g} fps within {fine['window_seconds']:g} s of each boundary "
                     f"and refined with one more request per batch (counts are known only after labeling)")
    if rep["where"] == "remote" and rep["images"]:
        lines.append("  note: the images above would leave this machine for the remote endpoint")
    if not rep["supports_images"] and rep["images"]:
        lines.append("  warning: this endpoint has supports_images: false; point llm.vision at a vision-capable profile")
    if not rep["model"]:
        lines.append("  warning: no model configured (llm.endpoints.<name>.model or its model_env variable)")
    return "\n".join(lines)


def analyze_inputs(ctx: Any) -> Any:
    s = VisionSettings.from_config(ctx.config)
    cat = ctx.catalog
    return {"assets": [(r["asset_id"], r["content_version"], r["media_type"]) for r in _eligible(cat, ctx.data.get("analyze_limit"))],
            "segments": [(r["segment_id"], r["content_version"]) for r in cat.list_segments()],
            "keys": [s.cache_key("video"), s.cache_key("photo")], "dup": s.duplicate_distance,
            "tx": _transcript_digest(ctx, s), "limit": ctx.data.get("analyze_limit"),
            "fine": rf.RefineSettings.from_config(ctx.config, s).stage_key()}


def _transcript_digest(ctx: Any, s: VisionSettings) -> str | None:
    """Changes when the transcript text the labeler would see changes (only relevant while it is sent at all)."""
    if not s.transcript_max_chars:
        return None
    return stable_hash([(r["transcript_id"], r["content_version"], r["text"]) for r in ctx.catalog.list_transcripts()] + [s.transcript_max_chars])


def _default_gateway(ctx: Any) -> Gateway:
    return make_gateway(ctx.config, ctx.workspace.root, "vision", logger=ctx.logger)


# ---------------------------------------------------------------- stage
HEIF_SUFFIXES = (".heic", ".heif")


def _photo_problem(it: Item, gateway: Gateway) -> str | None:
    """None when the photo can be sent; else why not. Decodes it through the same preparation the gateway will use (the result
    lands in the prepared-image cache), so one undecodable file fails only itself instead of sinking the whole batch request."""
    if not it.src.is_file():
        return "photo file is missing"
    from trip_vlog.llm.images import prepare_image
    from trip_vlog.llm.types import ImagePart, LLMInputError
    from trip_vlog.paths import long_path

    try:
        prepare_image(ImagePart(long_path(it.src)), max_side=gateway.image_max_side, quality=gateway.image_quality,
                      cache_dir=gateway.image_cache_dir)
    except LLMInputError as e:
        name = it.src.name
        if it.src.suffix.lower() in HEIF_SUFFIXES:
            return f"cannot decode {name}; HEIC is not supported by the installed image library"
        return f"cannot decode {name}: {str(e).rsplit(': ', 1)[-1]}"
    return None


def analyze_stage(ctx: Any) -> StageResult:
    t0 = time.perf_counter()
    cfg, cat, ws = ctx.config, ctx.catalog, ctx.workspace
    limit = ctx.data.get("analyze_limit")
    work = build_work(ctx, limit)
    s = work.settings
    rs = rf.RefineSettings.from_config(cfg, s)
    warnings: list[str] = []
    todo_v, todo_p = work.todo("video"), work.todo("photo")
    if (todo_v or todo_p) and not s.supports_images:
        raise LLMConfigError(f"endpoint {s.endpoint!r} has supports_images: false; point llm.vision at a vision-capable profile")
    root = ws.root
    lab: Labeler | None = None

    def prepare(it: Item) -> str | None:
        if it.kind == "photo":
            return _photo_problem(it, lab.gateway)
        err = fr.extract_segment_frames(ctx.ffmpeg, it.src, it.seg, it.frames, root, s.sampling)
        if err:
            return f"frame extraction failed: {err}"
        serr = fr.make_segment_sheet(it.frames, it.seg, root, str(it.sheet))
        if serr:
            if it.sheet_mode:
                return serr
            it.sheet = None
            if ctx.logger is not None:
                ctx.logger.event("analyze", Status.FAILED, asset_id=it.asset["asset_id"], segment_id=it.sid, phase="sheet", error=serr)
        return None

    def accept(it: Item) -> None:
        aid, cv = it.asset["asset_id"], it.asset["content_version"]
        cat.cache_put(aid, ANALYZER + it.sid, it.key, cv, {"label": it.label, "frames": it.frames, "contact_sheet": it.sheet,
                                                          "model": it.model, "kind": it.kind})
        cat.upsert_moments(aid, cv, [_moment(it, s, None)])

    gws: list[Gateway] = []

    def gateway() -> Gateway:
        if not gws:
            gws.append((gateway_factory or _default_gateway)(ctx))
        return gws[0]

    rst: rf.RefineStats | None = None
    try:
        if todo_v or todo_p:
            lab = Labeler(gateway(), s, root, prepare=prepare, accept=accept, logger=ctx.logger)
            if todo_v:
                lab.run(todo_v, "video")
            if todo_p:
                lab.run(todo_p, "photo")
        if rs.enabled:
            rst = _refine(ctx, work, rs, gateway, lab._answered if lab else 0)
        fallback = _finalize(ctx, work)
    finally:
        n_exported = cat.export_moments_jsonl(ws.moments_jsonl)
    st = lab.stats if lab else None
    summary: dict[str, Any] = {
        "endpoint": s.endpoint, "local": s.is_local, "model": s.model or None, "sampling_fps": s.sampling.fps,
        "max_frames_per_segment": s.sampling.max_frames, "input": s.input_mode, "assets": len(work.assets), "video_segments": len(work.videos), "photos": len(work.photos),
        "already_labeled": len(work.items) - len(todo_v) - len(todo_p),
        "labeled": len(todo_v) + len(todo_p) - fallback, "fallback_unlabeled": fallback,
        "requests": st.requests if st else 0, "response_cache_hits": st.cache_hits if st else 0,
        "retried_items": st.retried_items if st else 0, "rejected_items": st.rejected if st else 0,
        "unknown_ids_ignored": st.unknown_ids if st else 0,
        "input_tokens": st.usage.input_tokens if st else 0, "output_tokens": st.usage.output_tokens if st else 0,
        "moments_exported": n_exported, "elapsed_seconds": round(time.perf_counter() - t0, 3),
    }
    errors = [*(st.errors if st else []), *(rst.errors if rst else [])]
    if rst is not None:
        summary["fine_sampling"] = {
            "fps": rs.sampling.fps, "window_seconds": rs.window_seconds, "min_score": rs.min_score, "candidates": rst.candidates,
            "from_cache": rst.cached, "requests": rst.requests, "response_cache_hits": rst.cache_hits, "refined": rst.refined,
            "unchanged": rst.unchanged, "rejected": rst.rejected, "failed": len(rst.errors) - rst.rejected,
            "input_tokens": rst.usage.input_tokens, "output_tokens": rst.usage.output_tokens}
        if rst.errors:
            warnings.append(f"fine sampling could not refine {len(rst.errors)} moment(s); they keep their labeled range and are retried by the next run")
    if errors:
        summary["errors"] = errors[:MAX_ERRORS_IN_SUMMARY]
        summary["error_count"] = len(errors)
    if warnings:
        summary["warnings"] = warnings
    return StageResult(Status.PARTIAL if (fallback or warnings) else Status.SUCCEEDED, summary)


def _moment(it: Item, s: VisionSettings, dup_group: str | None, *, refined: bool = True) -> dict[str, Any]:
    if it.label is None:
        return _with_refs(it, unlabeled_moment(it.baseline, it.error, dup_group))
    sheet = it.sheet
    m = labeled_moment(it.baseline, it.seg, it.label, frames=it.frames, content_version=it.asset["content_version"],
                       model=it.model, prompt=s.prompt_info(it.kind), contact_sheet=sheet, duplicate_group=dup_group)
    return _with_refs(it, rf.apply_refinement(m, it.refinement) if refined else m)


def _refine(ctx: Any, work: Work, rs: rf.RefineSettings, gateway: Callable[[], Gateway], answered: int) -> rf.RefineStats:
    """Fine sampling (DESIGN §7.2 step 5): pick the best-scoring labeled video moments, reuse cached refinements, and ask for the rest.
    Every item gets `refine_key` (so its cache row survives a lower cap) and, when selected and refined, `refinement`."""
    cat = ctx.catalog
    pool: list[tuple[Item, float, float, float]] = []
    for it in work.videos:
        it.refinement = it.refine_key = None
        if it.label is None:
            continue
        m = _moment(it, work.settings, None, refined=False)
        a, b = m.get("source_in_seconds"), m.get("source_out_seconds")
        if rf.eligible(it, a, b):
            it.refine_key = rs.item_key(a, b)
            pool.append((it, float(it.label["interest"]), a, b))
    ranges = {id(p[0]): (p[2], p[3]) for p in pool}
    chosen = rf.select([(p[0], p[1]) for p in pool], rs)
    todo: list[rf.RefineItem] = []
    cached = 0
    for it in chosen:
        aid, cv = it.asset["asset_id"], it.asset["content_version"]
        rec = cat.cache_get(aid, rf.REFINE_ANALYZER + it.sid, it.refine_key, cv)
        if rec is not None:
            it.refinement, cached = rec, cached + 1
            continue
        ri = rf.make_refine_item(it, rs, *ranges[id(it)])
        if ri.windows is None:
            continue
        rf.plan_frames(ri, rs)
        todo.append(ri)

    def accept(ri: rf.RefineItem) -> None:
        it = ri.item
        rec = rf.make_record(ri, rs, ri.model)
        cat.cache_put(it.asset["asset_id"], rf.REFINE_ANALYZER + it.sid, ri.key, it.asset["content_version"], rec)
        it.refinement = rec
        cat.upsert_moments(it.asset["asset_id"], it.asset["content_version"], [_moment(it, work.settings, None)])

    stats = rf.RefineStats(candidates=len(chosen), cached=cached)
    if todo:
        if not rs.supports_images:
            raise LLMConfigError(f"endpoint {rs.endpoint!r} has supports_images: false; point llm.vision at a vision-capable profile")
        rf.Refiner(gateway(), rs, ctx.workspace.root, ctx.ffmpeg, accept=accept, logger=ctx.logger, answered=answered, stats=stats).run(todo)
    return stats


def _with_refs(it: Item, m: dict[str, Any]) -> dict[str, Any]:
    """transcript_refs follow the moment's final range (a labeled moment may be narrower than its segment)."""
    if it.tx_rows:
        m["transcript_refs"] = sc.refs_for(it.tx_rows, m.get("source_in_seconds"), m.get("source_out_seconds"))
    return m


def photo_groups(ctx: Any, photos: list[Item], distance: int) -> dict[str, str]:
    """asset_id -> duplicate_group. Hashes are cached per asset content_version."""
    hashes: dict[str, int] = {}
    for it in photos:
        aid, cv = it.asset["asset_id"], it.asset["content_version"]
        rec = ctx.catalog.cache_get(aid, PHASH_ANALYZER, PHASH_KEY, cv)
        if rec is not None:
            if rec["dhash"] is not None:
                hashes[aid] = int(rec["dhash"], 16)
            continue
        try:
            h = ph.dhash_or_flat(it.src)
        except Exception as e:  # noqa: BLE001 - undecodable pictures simply have no duplicate group
            ctx.logger.event("analyze", Status.SKIPPED, asset_id=aid, phase="dhash", message=f"{type(e).__name__}")
            continue
        ctx.catalog.cache_put(aid, PHASH_ANALYZER, PHASH_KEY, cv, {"dhash": None if h is None else f"{h:016x}"})
        if h is not None:   # a flat picture has no duplicate group
            hashes[aid] = h
    return ph.group_duplicates(hashes, distance)


def _finalize(ctx: Any, work: Work) -> int:
    """Rebuild every moment of the processed assets from the cached labels (idempotent), add photo duplicate groups,
    drop vision moments of segments that no longer exist and prune superseded cache rows. Returns the unlabeled count."""
    cat = ctx.catalog
    groups = photo_groups(ctx, work.photos, work.settings.duplicate_distance)
    by_asset: dict[str, list[Item]] = {}
    for it in work.items:
        by_asset.setdefault(it.asset["asset_id"], []).append(it)
    fallback = 0
    base_keys = {work.settings.cache_key("video"), work.settings.cache_key("photo")}
    for aid, items in by_asset.items():
        keys = base_keys | {it.key for it in items}  # items with transcript text have their own key
        cv = items[0].asset["content_version"]
        moments = []
        for it in items:
            moments.append(_moment(it, work.settings, groups.get(aid) if it.kind == "photo" else None))
            fallback += int(it.label is None)
        cat.upsert_moments(aid, cv, moments)
        current = {m["moment_id"] for m in moments}
        stale = [r["moment_id"] for r in cat.list_moments(aid)
                 if r["moment_id"] not in current and json.loads(r["data_json"] or "{}").get("source") == "llm"]
        cat.delete_moments(stale)
        cat.prune_cache(aid, ANALYZER, keys)
        cat.prune_cache(aid, rf.REFINE_ANALYZER, {it.refine_key for it in items if it.refine_key})
        if all(it.label is not None for it in items):
            cat.mark_analysis_complete(aid)
    return fallback
