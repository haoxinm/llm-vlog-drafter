"""The `audio` and `transcribe` stages (DESIGN §7.3, §7.5).

`audio`      one cheap ffmpeg pass per asset (cached by content_version + feature settings) -> loudness, silent / clipped /
             wind-likely flags and the active-audio ratio, stored on every segment (`features.audio`) and moment (`audio`,
             `audio_tags`). Independent of transcription.
`transcribe` per-asset transcripts through the configured backend, cached by content_version + backend identity (model, language,
             request parameters) + audio-extraction settings. Changing the target duration, story order or segment windows never
             re-runs it: the cache key has no segment or plan input. Transcript segments go to the catalog `transcripts` table and
             `media_index/transcripts.jsonl`; `transcript_refs` of overlapping moments are filled in. `speech_summary` is left null:
             verbatim words and inferred summaries are kept apart.

Both stages are idempotent and cheap when everything is cached, and their stage input hash includes the derived state they write
(per-segment audio flags, per-moment refs), so they re-run after the segment stage rebuilt segments or moments.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from trip_vlog.config import SpeechConfig
from trip_vlog.logging_utils import Status
from trip_vlog.media.segment import moment_id_for
from trip_vlog.speech import audio as au
from trip_vlog.speech import context as sc
from trip_vlog.speech.command_backend import CommandTranscriber
from trip_vlog.speech.http_backend import OpenAITranscriptions, resolve_speech_model
from trip_vlog.speech.parse import merge
from trip_vlog.speech.types import FATAL, SpeechBadRequestError, SpeechConfigError, SpeechConnectionError, SpeechError, SpeechTimeoutError, Transcription, TranscriptSegment
from trip_vlog.state import StageResult, stable_hash

TRANSCRIBE_VERSION = 1
ANALYZER = "transcript"
AUDIO_ANALYZER = "audio_profile"
MIN_TAIL_SECONDS = 5.0
MAX_CONSECUTIVE_UNREACHABLE = 3
MAX_EARLY_BAD_REQUESTS = 2   # HTTP 400s in a row before any request of the run succeeded: a configuration problem, not a bad file
MAX_ERRORS_IN_SUMMARY = 10

# Test hook: replace to inject a backend double (`lambda ctx: FakeBackend()`).
backend_factory: Callable[[Any], Any] | None = None


# ---------------------------------------------------------------- settings and identity
def backend_identity(sp: SpeechConfig, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What changes the transcript text. No URLs, header values, keys, timeouts or retry counts."""
    if sp.backend == "openai_transcriptions":
        e = sp.endpoint
        return {"backend": sp.backend, "model": resolve_speech_model(e, env) or "", "language": sp.language, "format": e.response_format,
                "granularities": list(e.timestamp_granularities), "params": e.params}
    if sp.backend == "command":
        c = sp.command
        return {"backend": sp.backend, "executable": Path(c.executable or "").name, "args": list(c.args), "format": c.output_format,
                "language": sp.language}
    return {"backend": "none"}


def cache_key(sp: SpeechConfig, env: Mapping[str, str] | None = None) -> str:
    audio: dict[str, Any] = {"format": sp.audio_format, "rate": au.SAMPLE_RATE, "channels": 1}
    if sp.backend == "openai_transcriptions":
        audio["chunk_seconds"] = sp.chunk_seconds   # chunk boundaries change the text; irrelevant to a local command (whole file)
    return stable_hash({"v": TRANSCRIBE_VERSION, "backend": backend_identity(sp, env), "audio": audio})


def make_backend(sp: SpeechConfig, env: Mapping[str, str] | None = None, transport: Any = None) -> Any:
    if sp.backend == "openai_transcriptions":
        return OpenAITranscriptions(sp.endpoint, language=sp.language, env=env, transport=transport)
    if sp.backend == "command":
        return CommandTranscriber(sp.command, language=sp.language)
    raise SpeechConfigError("speech.backend is 'none'")


def _public_url(url: str) -> str:
    from trip_vlog.llm.check import public_url

    return public_url(url)


# ---------------------------------------------------------------- planning
@dataclass
class Entry:
    row: Any
    status: str                 # "todo" | "cached" | "skip"
    reason: str = ""            # for skips: no_audio_stream | silent | not_audio
    active_seconds: float | None = None
    result: dict[str, Any] | None = None

    @property
    def aid(self) -> str:
        return self.row["asset_id"]

    @property
    def duration(self) -> float:
        return float(self.row["duration_seconds"] or 0.0)


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    key: str = ""

    def by(self, status: str) -> list[Entry]:
        return [e for e in self.entries if e.status == status]

    def skipped(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.by("skip"):
            out[e.reason] = out.get(e.reason, 0) + 1
        return dict(sorted(out.items()))


def limit_for(ctx: Any) -> int | None:
    return ctx.data.get("transcribe_limit") or ctx.data.get("analyze_limit")


def _asset_rows(cat: Any, limit: int | None) -> list[Any]:
    rows = [r for r in cat.list_assets() if r["probe_status"] == "ok" and r["media_type"] in ("video", "audio", "image")]
    return rows[:limit] if limit else rows


def active_seconds(cat: Any, row: Any) -> float:
    """Seconds of non-silent audio according to the segmenter's silence detection (unknown counts as active)."""
    total = 0.0
    for s in cat.list_segments(row["asset_id"]):
        if s["content_version"] != row["content_version"] or s["start_seconds"] is None:
            continue
        feats = json.loads(s["data_json"]).get("features", {})
        if feats.get("silent") is not True:
            total += s["end_seconds"] - s["start_seconds"]
    return total


def build_plan(ctx: Any, env: Mapping[str, str] | None = None) -> Plan:
    cat, sp = ctx.catalog, ctx.config.speech
    key = cache_key(sp, env)
    plan = Plan(key=key)
    for row in _asset_rows(cat, limit_for(ctx)):
        mt = row["media_type"]
        if mt == "image":
            continue
        if mt == "video" and not (row["audio_streams"] or 0):
            plan.entries.append(Entry(row, "skip", "no_audio_stream"))
            continue
        act = active_seconds(cat, row) if mt == "video" or cat.list_segments(row["asset_id"]) else float(row["duration_seconds"] or 0.0)
        cached = cat.cache_get(row["asset_id"], ANALYZER, key, row["content_version"])
        if cached is not None:
            plan.entries.append(Entry(row, "cached", active_seconds=act, result=cached))
        elif act <= 0 or act < sp.min_speech_seconds:
            plan.entries.append(Entry(row, "skip", "silent", act))
        else:
            plan.entries.append(Entry(row, "todo", active_seconds=act))
    return plan


def chunk_plan(duration: float, chunk_seconds: float) -> list[tuple[float, float]]:
    """[(start, length)] covering the clip; a short tail is folded into the previous chunk."""
    if duration <= chunk_seconds:
        return [(0.0, duration)]
    n = math.ceil(duration / chunk_seconds)
    if n > 1 and duration - (n - 1) * chunk_seconds < MIN_TAIL_SECONDS:
        n -= 1
    return [(i * chunk_seconds, (duration - i * chunk_seconds) if i == n - 1 else chunk_seconds) for i in range(n)]


# ---------------------------------------------------------------- dry run
def dry_run_report(ctx: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    sp = ctx.config.speech
    base: dict[str, Any] = {"enabled": sp.enabled, "backend": sp.backend, "local": sp.is_local}
    if sp.backend == "openai_transcriptions":
        base.update(endpoint=_public_url(sp.endpoint.url), model=resolve_speech_model(sp.endpoint, env))
    elif sp.backend == "command":
        base.update(executable=Path(sp.command.executable or "").name)
    if not sp.active:
        base["reason"] = "speech.enabled is false" if not sp.enabled else "speech.backend is none"
        return base
    plan = build_plan(ctx, env)
    todo = plan.by("todo")
    chunked = sp.backend == "openai_transcriptions"
    base.update(assets=len(plan.entries), cached=len(plan.by("cached")), to_transcribe=len(todo), skipped=plan.skipped(),
                audio_minutes=round(sum(e.duration for e in todo) / 60.0, 2),
                requests=sum(len(chunk_plan(e.duration, sp.chunk_seconds)) if chunked else 1 for e in todo),
                audio_format=sp.audio_format, limit=limit_for(ctx))
    return base


def format_dry_run(rep: dict[str, Any]) -> str:
    where = "local" if rep["local"] else "remote"
    if rep["backend"] == "none" or "reason" in rep:
        return f"transcribe dry run: skipped ({rep.get('reason', 'speech.backend is none')}); nothing would be extracted or sent"
    target = rep.get("endpoint") or rep.get("executable")
    lines = [
        f"transcribe dry run (nothing is extracted or sent): backend {rep['backend']} -> {target} ({where})"
        + (f", model {rep['model']}" if rep.get("model") else (", no model configured" if rep["backend"] == "openai_transcriptions" else "")),
        f"  assets with audio     : {rep['assets']}" + (f" (--limit {rep['limit']})" if rep.get("limit") else ""),
        f"  already transcribed   : {rep['cached']}",
        f"  to transcribe         : {rep['to_transcribe']} (about {rep['audio_minutes']:g} min of audio, {rep['requests']} request(s)/run(s), {rep['audio_format']})",
    ]
    if rep["skipped"]:
        lines.append("  skipped               : " + ", ".join(f"{n} {why}" for why, n in rep["skipped"].items()))
    if not rep["local"] and rep["to_transcribe"]:
        lines.append("  note: the audio above would leave this machine for the remote endpoint")
    return "\n".join(lines)


# ---------------------------------------------------------------- transcribe stage
def _record(tr: Transcription, backend: str, model: str | None) -> dict[str, Any]:
    return {"segments": [{"start": s.start, "end": s.end, "text": s.text, "confidence": s.confidence, "avg_logprob": s.avg_logprob,
                          "no_speech_prob": s.no_speech_prob} for s in tr.segments],
            "language": tr.language, "timestamps": tr.timestamps, "backend": backend, "model": model}


def _segments(rec: Mapping[str, Any]) -> list[TranscriptSegment]:
    return [TranscriptSegment(s["start"], s["end"], s["text"], s.get("confidence"), s.get("avg_logprob"), s.get("no_speech_prob"))
            for s in rec["segments"]]


def _transcribe_asset(ctx: Any, backend: Any, e: Entry) -> Transcription:
    sp, ws = ctx.config.speech, ctx.workspace
    src = ctx.paths.media_root / Path(*e.row["relative_path"].split("/"))
    if not src.is_file():
        raise SpeechError("the source file is missing")
    cvh = hashlib.sha1(str(e.row["content_version"]).encode("utf-8")).hexdigest()[:12]
    chunks = chunk_plan(e.duration, sp.chunk_seconds) if backend.name == "openai_transcriptions" else [(0.0, e.duration)]
    parts: list[tuple[float, Transcription]] = []
    made: list[Path] = []
    try:
        for i, (start, length) in enumerate(chunks):
            dest = ws.audio_dir / e.aid / f"{cvh}_{sp.audio_format}_{i}.{sp.audio_format}"
            if not (sp.keep_audio and dest.is_file()):
                err = au.extract_audio(ctx.ffmpeg, src, dest, sp.audio_format, start=start if len(chunks) > 1 else None,
                                       duration=length if len(chunks) > 1 else None, timeout=max(120.0, 2.0 * length))
                if err:
                    raise SpeechError(err)
            made.append(dest)
            parts.append((start, backend.transcribe(dest, length or None)))
    finally:
        if not sp.keep_audio:
            for p in made:
                p.unlink(missing_ok=True)
            try:
                (ws.audio_dir / e.aid).rmdir()
            except OSError:
                pass
    return merge(parts)


def _store(cat: Any, e: Entry, rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    recs = sc.to_records(e.aid, e.row["content_version"], _segments(rec), language=rec.get("language"),
                         timestamps=rec.get("timestamps", "segment"), backend=rec.get("backend", ""), model=rec.get("model"))
    cat.replace_transcripts(e.aid, e.row["content_version"], recs)
    return recs


def apply_to_moments(cat: Any, row: Any, rows: list[dict[str, Any]]) -> int:
    """Set `transcript_refs` on every moment of the asset from the stored transcript. Returns the moments that have refs."""
    n = 0
    for m in cat.list_moments(row["asset_id"]):
        refs = sc.refs_for(rows, m["source_in_seconds"], m["source_out_seconds"])
        n += bool(refs)
        cat.update_moment_fields(m["moment_id"], {"transcript_refs": refs})
    return n


def transcribe_inputs(ctx: Any) -> Any:
    sp, cat = ctx.config.speech, ctx.catalog
    base: dict[str, Any] = {"active": sp.active, "limit": limit_for(ctx), "ffmpeg": ctx.ffmpeg is not None}
    if not sp.active:
        return base
    return {**base, "key": cache_key(sp), "min": sp.min_speech_seconds,
            "assets": [(r["asset_id"], r["content_version"], r["audio_streams"], r["duration_seconds"]) for r in _asset_rows(cat, limit_for(ctx))],
            "refs": stable_hash([(m["moment_id"], json.loads(m["data_json"] or "{}").get("transcript_refs")) for m in cat.list_moments()])}


def transcribe_stage(ctx: Any) -> StageResult:
    t0 = time.perf_counter()
    cat, sp, ws = ctx.catalog, ctx.config.speech, ctx.workspace
    if not sp.active:
        reason = "speech.enabled is false" if not sp.enabled else "speech.backend is none (see README \"Speech transcription\")"
        return StageResult(Status.SKIPPED, {"skipped": True, "reason": reason, "backend": sp.backend})
    plan = build_plan(ctx)
    todo = plan.by("todo")
    summary: dict[str, Any] = {"backend": sp.backend, "local": sp.is_local, "assets": len(plan.entries), "cached": len(plan.by("cached")),
                               "skipped_assets": plan.skipped(), "transcribed": 0, "failed": 0, "segments": 0}
    failures: list[dict[str, str]] = []
    fatal: str | None = None
    unreachable = 0
    bad_requests = 0
    audio_seconds = 0.0
    n_refs = 0
    try:
        if todo:
            if ctx.ffmpeg is None:
                raise SpeechConfigError("ffmpeg is required to extract audio for transcription but was not found (see README, FFmpeg)")
            backend = (backend_factory or (lambda c: make_backend(c.config.speech)))(ctx)
            summary["model"] = getattr(backend, "model", None)
            for e in todo:
                try:
                    tr = _transcribe_asset(ctx, backend, e)
                except FATAL as ex:
                    fatal = f"{type(ex).__name__}: {ex}"
                    failures.append({"path": e.row["relative_path"], "error": fatal})
                    ctx.logger.event("transcribe", Status.FAILED, asset_id=e.aid, error=fatal)
                    break
                except SpeechError as ex:
                    summary["failed"] += 1
                    msg = f"{type(ex).__name__}: {ex}"[:400]
                    failures.append({"path": e.row["relative_path"], "error": msg})
                    ctx.logger.event("transcribe", Status.FAILED, asset_id=e.aid, error=msg, path=e.row["relative_path"])
                    unreachable = unreachable + 1 if isinstance(ex, (SpeechConnectionError, SpeechTimeoutError)) else 0
                    bad_requests = bad_requests + 1 if isinstance(ex, SpeechBadRequestError) else 0
                    if bad_requests >= MAX_EARLY_BAD_REQUESTS and not summary["transcribed"]:
                        fatal = (f"stopping: the first {bad_requests} requests were rejected as invalid and none succeeded, so this is "
                                 f"probably a configuration problem (speech.endpoint model/params/response_format), not the files. Server said: {msg}")
                        break
                    if unreachable >= MAX_CONSECUTIVE_UNREACHABLE:
                        fatal = f"stopping after {unreachable} consecutive connection failures: {msg}"
                        break
                    continue
                unreachable = bad_requests = 0
                rec = _record(tr, backend.name, getattr(backend, "model", None))
                cat.cache_put(e.aid, ANALYZER, plan.key, e.row["content_version"], rec)
                cat.prune_cache(e.aid, ANALYZER, {plan.key})
                e.status, e.result = "cached", rec
                summary["transcribed"] += 1
                audio_seconds += e.duration
                ctx.logger.event("transcribe", Status.SUCCEEDED, asset_id=e.aid, segments=len(tr.segments), cache="miss",
                                 timestamps=tr.timestamps, path=e.row["relative_path"])
    except SpeechConfigError as ex:   # unusable configuration or no ffmpeg: nothing was attempted
        fatal = f"{type(ex).__name__}: {ex}"
    try:
        # bring the catalog in line with the cache (idempotent), also for assets that are now skipped
        for e in plan.entries:
            rows: list[dict[str, Any]] = []
            if e.result is not None:
                rows = _store(cat, e, e.result)
                summary["segments"] += len(rows)
            elif e.status == "skip":
                cat.replace_transcripts(e.aid, e.row["content_version"], [])
            else:
                continue  # failed this run: keep whatever is stored
            n_refs += apply_to_moments(cat, e.row, rows)
    finally:
        summary["moments_with_transcript"] = n_refs
        summary["transcripts_exported"] = cat.export_transcripts_jsonl(ws.transcripts_jsonl)
        cat.export_moments_jsonl(ws.moments_jsonl)
        try:
            ws.audio_dir.rmdir()   # only succeeds when empty (nothing kept)
        except OSError:
            pass
    summary["audio_minutes"] = round(audio_seconds / 60.0, 2)
    summary["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
    if failures:
        summary["errors"] = failures[:MAX_ERRORS_IN_SUMMARY]
        summary["error_count"] = len(failures)
    if fatal:
        return StageResult(Status.FAILED, summary, fatal)
    return StageResult(Status.PARTIAL if failures else Status.SUCCEEDED, summary)


# ---------------------------------------------------------------- audio stage
def _audio_assets(cat: Any, limit: int | None) -> list[Any]:
    return [r for r in _asset_rows(cat, limit) if r["media_type"] in ("video", "audio")]


def audio_inputs(ctx: Any) -> Any:
    cat, media = ctx.catalog, ctx.config.media
    if not media.audio_features:
        return {"enabled": False}
    state = []
    for r in _audio_assets(cat, limit_for(ctx)):
        for s in cat.list_segments(r["asset_id"]):
            state.append((s["segment_id"], s["content_version"], "audio" in json.loads(s["data_json"]).get("features", {})))
    return {"enabled": True, "key": au.features_key(media.silence_threshold_db), "ffmpeg": ctx.ffmpeg is not None, "segments": state,
            "assets": [(r["asset_id"], r["content_version"], r["audio_streams"]) for r in _audio_assets(cat, limit_for(ctx))]}


def audio_stage(ctx: Any) -> StageResult:
    cat, media = ctx.catalog, ctx.config.media
    if not media.audio_features:
        return StageResult(Status.SKIPPED, {"skipped": True, "reason": "media.audio_features is false"})
    key = au.features_key(media.silence_threshold_db)
    measured = hits = no_audio = failed = segs_done = 0
    errors: list[dict[str, str]] = []
    for row in _audio_assets(cat, limit_for(ctx)):
        aid, cv = row["asset_id"], row["content_version"]
        segs = [json.loads(s["data_json"]) for s in cat.list_segments(aid) if s["content_version"] == cv]
        if not segs:
            continue
        profile: dict[str, Any] | None = None
        if row["media_type"] == "video" and not (row["audio_streams"] or 0):
            no_audio += 1
        else:
            profile = cat.cache_get(aid, AUDIO_ANALYZER, key, cv)
            if profile is not None:
                hits += 1
            elif ctx.ffmpeg is None:
                failed += 1
                errors.append({"path": row["relative_path"], "error": "ffmpeg not found"})
                continue
            else:
                src = ctx.paths.media_root / Path(*row["relative_path"].split("/"))
                try:
                    profile = au.measure_audio(ctx.ffmpeg, src, row["duration_seconds"])
                except RuntimeError as ex:
                    failed += 1
                    errors.append({"path": row["relative_path"], "error": str(ex)[:300]})
                    ctx.logger.event("audio", Status.FAILED, asset_id=aid, error=str(ex), path=row["relative_path"])
                    continue
                cat.cache_put(aid, AUDIO_ANALYZER, key, cv, profile)
                cat.prune_cache(aid, AUDIO_ANALYZER, {key})
                measured += 1
        for s in segs:
            if profile is None:
                a = au.no_audio()
            else:
                a = au.summarize(profile["windows"], s["start_seconds"], s["end_seconds"], media.silence_threshold_db)
                a["asset_loudness_lufs"] = profile.get("lufs")
            s.setdefault("features", {})["audio"] = a
        cat.replace_segments(aid, cv, segs)
        for s in segs:
            cat.update_moment_fields(moment_id_for(s["segment_id"]), {"features": s["features"], "audio": s["features"]["audio"],
                                                                      "audio_tags": s["features"]["audio"].get("audio_tags", [])})
        segs_done += len(segs)
    n = cat.export_moments_jsonl(ctx.workspace.moments_jsonl)
    summary: dict[str, Any] = {"assets_measured": measured, "cache_hits": hits, "no_audio_stream": no_audio, "failed": failed,
                               "segments": segs_done, "moments_exported": n}
    if errors:
        summary["errors"] = errors[:MAX_ERRORS_IN_SUMMARY]
    return StageResult(Status.PARTIAL if failed else Status.SUCCEEDED, summary)
