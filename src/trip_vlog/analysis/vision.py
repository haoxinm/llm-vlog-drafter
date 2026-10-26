"""Vision labeling of video segments and photos: settings, prompts, batching, validation, retry and fallback (DESIGN §7.2, §9).

Flow per batch: the request carries each segment's frames together, announced by text lines with the segment ID, frame index
and source time; the reply is validated against the schema (by the gateway) and then referentially (here). Valid items are
handed to `accept` immediately (the stage persists them, so a crash resumes at the next batch). Items the reply omitted or
got wrong are re-asked once, in a fresh request that bypasses the response cache; whatever still fails falls back to the
deterministic baseline moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trip_vlog.analysis import frames as fr
from trip_vlog.analysis.labels import (
    EDITORIAL_TAGS,
    LABEL_VERSION,
    photo_labels_schema,
    schema_hash,
    segment_labels_schema,
    validate_label,
)
from trip_vlog.config import Config
from trip_vlog.llm.adapters import fingerprint_extra
from trip_vlog.llm.factory import resolve_model
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import (
    ImagePart,
    LLMBadRequestError,
    LLMConfigError,
    LLMInputError,
    LLMRefusalError,
    LLMRequest,
    LLMSchemaError,
    LLMTruncatedError,
    TextPart,
    Usage,
)
from trip_vlog.llm.untrusted import PREAMBLE, untrusted_block
from trip_vlog.logging_utils import RunLogger, Status
from trip_vlog.prompts import PromptTemplate, load_template
from trip_vlog.speech.context import text_digest
from trip_vlog.state import stable_hash

ANALYSIS_VERSION = 1
TRANSCRIPT_CONTEXT_VERSION = 1   # bump when the wording of the transcript block below changes
TRANSCRIPT_GUIDANCE = ("Spoken words heard in this clip (verbatim, possibly mis-heard). Use them only to help identify what is "
                       "happening for `event`; keep `description` and `visible_entities` to what is visible, and never repeat names, "
                       "places or claims from the words unless the frames show them too.")
TOKENS_PER_ITEM = 450      # output budget per labelled segment/photo
TOKENS_BASE = 300
MAX_EARLY_BAD_REQUESTS = 2   # HTTP 400s in a row before any request of the run succeeded: a configuration problem, not bad items

# Errors of one request that leave the others usable: the affected items are retried once, then fall back.
RECOVERABLE = (LLMSchemaError, LLMTruncatedError, LLMRefusalError, LLMBadRequestError, LLMInputError)


@dataclass(frozen=True)
class VisionSettings:
    sampling: fr.SamplingSettings
    input_mode: str
    batch_max_images: int
    max_items_per_request: int
    send_filenames: bool
    model: str
    provider_extra: Mapping[str, Any]
    endpoint: str
    is_local: bool
    supports_images: bool
    max_tokens_cap: int
    image_max_side: int
    image_quality: int
    duplicate_distance: int
    analyst: PromptTemplate
    segment_prompt: PromptTemplate
    photo_prompt: PromptTemplate
    transcript_max_chars: int = 0   # >0: verbatim transcript text of a segment is sent (as untrusted data), cut at this length

    @classmethod
    def from_config(cls, config: Config, env: Mapping[str, str] | None = None) -> "VisionSettings":
        name, ep = config.llm.endpoint_for("vision")
        m = config.media
        max_items = max(1, (ep.max_tokens - TOKENS_BASE) // TOKENS_PER_ITEM)
        return cls(
            sampling=fr.SamplingSettings.from_config(config, m.vision_batch_max_images), input_mode=m.vision_input,
            batch_max_images=m.vision_batch_max_images, max_items_per_request=max_items, send_filenames=m.vision_send_filenames,
            model=resolve_model(ep, env) or "", provider_extra=fingerprint_extra(ep), endpoint=name, is_local=ep.is_local,
            supports_images=ep.supports_images, max_tokens_cap=ep.max_tokens, image_max_side=config.llm.image_max_side,
            image_quality=config.llm.image_quality, duplicate_distance=m.photo_duplicate_distance,
            analyst=load_template("media_analyst", 1), segment_prompt=load_template("segment_labels", 1),
            photo_prompt=load_template("photo_labels", 1),
            transcript_max_chars=config.speech.context_max_chars if config.speech.enabled else 0)

    # -- identity ------------------------------------------------------------------------------
    def prompt_info(self, kind: str) -> dict[str, Any]:
        t = self.segment_prompt if kind == "video" else self.photo_prompt
        return {"id": t.id, "version": t.version}

    def schema(self, kind: str) -> dict[str, Any]:
        return segment_labels_schema() if kind == "video" else photo_labels_schema()

    def system(self, kind: str) -> str:
        t = self.segment_prompt if kind == "video" else self.photo_prompt
        return f"{self.analyst.text}\n\n{t.text}\n\nAllowed editorial_tags: {', '.join(EDITORIAL_TAGS)}."

    def cache_key(self, kind: str) -> str:
        """Per-item analysis cache key (DESIGN §7.5): prompts, schema, model, endpoint api/params, sampling and input settings.
        The asset content_version is checked by the cache row itself and the segment bounds are part of the segment ID."""
        t = self.segment_prompt if kind == "video" else self.photo_prompt
        ident: dict[str, Any] = {
            "v": ANALYSIS_VERSION, "label_v": LABEL_VERSION, "kind": kind,
            "analyst": [self.analyst.id, self.analyst.version, self.analyst.sha256], "task": [t.id, t.version, t.sha256],
            "schema": schema_hash(self.schema(kind)), "model": self.model, "provider": dict(self.provider_extra),
            "image": [self.image_max_side, self.image_quality], "filenames": self.send_filenames,
        }
        if kind == "video":
            ident.update(sampling=self.sampling.key(), input=self.input_mode)
        return stable_hash(ident)


    def item_key(self, base: str, transcript: str) -> str:
        """Per-item cache key: the kind's key, extended by the transcript text only for segments that have some (so segments
        without speech keep their existing cache entries when transcription is switched on)."""
        if not transcript or not self.transcript_max_chars:
            return base
        return stable_hash({"base": base, "transcript": text_digest(transcript, self.transcript_max_chars), "tv": TRANSCRIPT_CONTEXT_VERSION})


# ---------------------------------------------------------------- work items
@dataclass
class Item:
    kind: str                       # "video" | "photo"
    asset: Mapping[str, Any]        # catalog row
    seg: dict[str, Any]             # segment data (data_json)
    baseline: dict[str, Any]        # deterministic baseline moment for this segment
    src: Path                       # original file
    key: str
    frames: list[dict[str, Any]] = field(default_factory=list)   # planned frame records (video)
    sheet: str | None = None        # workspace-relative contact sheet (video)
    label: dict[str, Any] | None = None
    model: str = ""
    error: str | None = None
    cached: bool = False
    singleton: bool = False         # re-ask alone (its batch failed as a whole)
    sheet_mode: bool = False        # send the contact sheet instead of the individual frames
    transcript: str = ""            # verbatim speech overlapping this segment (video), "" if none
    tx_rows: list[dict[str, Any]] = field(default_factory=list)   # transcript records of the asset (for transcript_refs)

    @property
    def sid(self) -> str:
        return self.seg["segment_id"]

    @property
    def n_images(self) -> int:
        return 1 if self.kind == "photo" or self.sheet_mode else len(self.frames)


def make_batches(items: Sequence[Item], max_images: int, max_items: int) -> list[list[Item]]:
    """Greedy, order-preserving batches. A segment's images are never split; one that alone exceeds the limit gets its own request."""
    batches: list[list[Item]] = []
    cur: list[Item] = []
    imgs = 0
    for it in items:
        if it.singleton:
            batches.append([it])
            continue
        n = it.n_images
        if cur and (imgs + n > max_images or len(cur) >= max_items):
            batches.append(cur)
            cur, imgs = [], 0
        cur.append(it)
        imgs += n
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------- request building
def _orientation(asset: Mapping[str, Any]) -> str:
    w, h = asset["display_width"] or asset["width"], asset["display_height"] or asset["height"]
    if not w or not h:
        return "unknown orientation"
    return "portrait" if h > w else "landscape" if w > h else "square"


def build_request(settings: VisionSettings, kind: str, batch: Sequence[Item], root: Path, note: str | None = None) -> LLMRequest:
    """The LLMRequest for one batch. Text derived from media (file names) is wrapped as untrusted data; GPS is never included."""
    parts: list[Any] = []
    noun = "video segment(s)" if kind == "video" else "photo(s)"
    parts.append(TextPart(f"{PREAMBLE}\n\nLabel the {len(batch)} {noun} below. IDs, in order: {', '.join(i.sid for i in batch)}."))
    for it in batch:
        head = [f"=== {'Segment' if kind == 'video' else 'Photo'} {it.sid} ==="]
        if settings.send_filenames:
            head.append(untrusted_block("filename", Path(it.asset["relative_path"]).name, max_chars=200))
        if kind == "video" and it.transcript and settings.transcript_max_chars:
            head.append(TRANSCRIPT_GUIDANCE)
            head.append(untrusted_block("transcript", it.transcript, max_chars=settings.transcript_max_chars))
        if kind == "video":
            dur = (it.seg["end_seconds"] - it.seg["start_seconds"])
            if it.sheet_mode:
                head.append(f"Video clip, {dur:.1f} s long, {_orientation(it.asset)}. One contact sheet follows with the {len(it.frames)} sampled "
                            f"frame(s) in time order; each tile is labelled '#index t=seconds'. Use those indices for best_frames.")
            else:
                head.append(f"Video clip, {dur:.1f} s long, {_orientation(it.asset)}. {len(it.frames)} frame(s) follow in time order.")
        else:
            w, h = it.asset["display_width"] or it.asset["width"], it.asset["display_height"] or it.asset["height"]
            head.append(f"Still photo{f', {w}x{h}' if w and h else ''}, {_orientation(it.asset)}.")
        parts.append(TextPart("\n".join(head)))
        if kind == "photo":
            parts.append(ImagePart(_long(it.src)))
        elif it.sheet_mode:
            parts.append(ImagePart(root / str(it.sheet), identity=_identity(settings, it, "sheet", [fr.SHEET_TILE_W, *(f["frame"] for f in it.frames)])))
        else:
            for f in it.frames:
                parts.append(TextPart(f"Segment {it.sid} | frame {f['index']} | t={f['timestamp_seconds']:.2f}s"))
                parts.append(ImagePart(root / f["artifact"], identity=_identity(settings, it, "frame", f["frame"])))
    if note:
        parts.append(TextPart(note))
    t = settings.segment_prompt if kind == "video" else settings.photo_prompt
    max_tokens = min(TOKENS_PER_ITEM * len(batch) + TOKENS_BASE, settings.max_tokens_cap)
    return LLMRequest(t.id, str(t.version), settings.system(kind), tuple(parts), settings.schema(kind), max_tokens)


def _identity(settings: VisionSettings, it: Item, what: str, ref: Any) -> str:
    """Stable name of an ffmpeg-derived image for the request fingerprint: source content version, which frame (or the sheet),
    the sampling settings (which include the downscale/quality) -- never the machine-dependent JPEG bytes."""
    return stable_hash({"what": what, "cv": it.asset["content_version"], "ref": ref, "sampling": settings.sampling.key()})


def _long(p: Path) -> Path:
    from trip_vlog.paths import long_path

    return long_path(p)


def retry_note(items: Sequence[Item]) -> str:
    lines = [f"- {it.sid}: {it.error or 'missing from the reply'}" for it in items]
    return ("Correction: an earlier reply for the segments above was missing or invalid. Return one complete entry for every ID listed "
            "at the top, copying the IDs exactly and using only frame indices that exist.\n" + "\n".join(lines))


# ---------------------------------------------------------------- labeling loop
@dataclass
class RunStats:
    requests: int = 0
    cache_hits: int = 0
    retried_items: int = 0
    rejected: int = 0
    unknown_ids: int = 0
    usage: Usage = field(default_factory=Usage)
    errors: list[dict[str, str]] = field(default_factory=list)


class Labeler:
    """Runs batches through `gateway`; calls `prepare(item) -> error|None` before a request and `accept(item)` after validation."""

    def __init__(self, gateway: Gateway, settings: VisionSettings, root: Path, *, prepare: Callable[[Item], str | None],
                 accept: Callable[[Item], None], logger: RunLogger | None = None):
        self.gateway = gateway
        self.retry_gateway = Gateway(gateway.provider, schema_retries=gateway.schema_retries, image_max_side=gateway.image_max_side,
                                     image_quality=gateway.image_quality, image_cache_dir=gateway.image_cache_dir, cache=None,
                                     recorder=gateway.recorder, logger=gateway.logger)
        self.settings, self.root, self.prepare, self.accept, self.logger = settings, root, prepare, accept, logger
        self.stats = RunStats()
        self._answered = 0       # requests of this run the server accepted (cache hits do not count)
        self._bad_streak = 0     # consecutive bad-request rejections

    def _error(self, it: Item, msg: str, *, stage: str) -> None:
        it.error = msg
        self.stats.errors.append({"segment_id": it.sid, "asset_id": it.asset["asset_id"], "path": it.asset["relative_path"], "error": msg})
        if self.logger is not None:
            self.logger.event("analyze", Status.FAILED, asset_id=it.asset["asset_id"], segment_id=it.sid, phase=stage, error=msg)

    def run(self, items: Sequence[Item], kind: str) -> None:
        pending = list(items)
        for round_no in (1, 2):
            if not pending:
                return
            gw = self.gateway if round_no == 1 else self.retry_gateway
            if round_no == 2:
                self.stats.retried_items += len(pending)
            ready = []
            for it in pending:
                err = self.prepare(it)
                if err:
                    it.error = err  # nothing to retry: the input itself is unusable
                    self.stats.errors.append({"segment_id": it.sid, "asset_id": it.asset["asset_id"], "path": it.asset["relative_path"], "error": err})
                    if self.logger is not None:
                        self.logger.event("analyze", Status.FAILED, asset_id=it.asset["asset_id"], segment_id=it.sid, phase="prepare", error=err)
                else:
                    ready.append(it)
            failed: list[Item] = []
            for batch in make_batches(ready, self.settings.batch_max_images, self.settings.max_items_per_request):
                failed.extend(self._batch(gw, kind, batch, retry=round_no == 2))
            pending = failed

    def _batch(self, gw: Gateway, kind: str, batch: list[Item], *, retry: bool) -> list[Item]:
        note = retry_note(batch) if retry else None
        req = build_request(self.settings, kind, batch, self.root, note)
        self.stats.requests += 1
        try:
            resp = gw.complete(req)
        except RECOVERABLE as e:
            self._bad_streak = self._bad_streak + 1 if isinstance(e, LLMBadRequestError) else 0
            if self._bad_streak >= MAX_EARLY_BAD_REQUESTS and not self._answered:
                # every request so far was refused outright: the endpoint settings are wrong, retrying each item would only repeat it
                raise LLMConfigError(f"stopping: the first {self._bad_streak} requests were rejected as invalid and none succeeded, so this is "
                                     f"probably an endpoint configuration problem (model, params, max_tokens_field, structured_output). "
                                     f"Server said: {str(e)[:300]}") from None
            msg = f"{type(e).__name__}: {e}"[:300]
            for it in batch:
                self._error(it, msg, stage="request")
                it.singleton = len(batch) > 1
            return list(batch)
        # any other LLMError (auth, connection, config, replay miss, ...) is fatal and propagates: progress so far is persisted
        self._bad_streak = 0
        self._answered += 0 if resp.cached else 1
        self.stats.usage = self.stats.usage + (resp.usage if not resp.cached else Usage())
        self.stats.cache_hits += int(resp.cached)
        by_id: dict[str, dict[str, Any]] = {}
        ids = {it.sid for it in batch}
        for entry in resp.parsed["segments"]:
            sid = entry.get("segment_id")
            if sid not in ids:
                self.stats.unknown_ids += 1
                self.stats.errors.append({"segment_id": str(sid)[:80], "asset_id": "", "path": "", "error": "reply contained an unknown segment_id; ignored"})
                if self.logger is not None:
                    self.logger.event("analyze", Status.FAILED, segment_id=str(sid)[:80], phase="validate", error="unknown segment_id in reply")
            else:
                by_id.setdefault(sid, entry)
        failed = []
        for it in batch:
            entry = by_id.get(it.sid)
            if entry is None:
                self._error(it, "segment missing from the reply", stage="validate")
                self.stats.rejected += 1
                failed.append(it)
                continue
            label, err = validate_label(entry, n_frames=len(it.frames) if kind == "video" else None)
            if err or label is None:
                self._error(it, f"invalid label: {err}", stage="validate")
                self.stats.rejected += 1
                failed.append(it)
                continue
            it.label, it.model, it.error = label, resp.model, None
            self.accept(it)
        if any(it.error for it in failed) and gw.cache is not None:
            gw.cache.discard(resp.fingerprint)   # schema-valid but unusable: later runs must ask again, not reuse it
        if self.logger is not None:
            self.logger.event("analyze", Status.SUCCEEDED, batch_size=len(batch), images=sum(i.n_images for i in batch),
                              labeled=len(batch) - len(failed), cache="hit" if resp.cached else "miss", kind=kind)
        return failed
