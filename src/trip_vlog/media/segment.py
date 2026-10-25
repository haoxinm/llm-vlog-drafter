"""Deterministic candidate segmentation (DESIGN §7.2 step 2) and baseline moments (Phase 2).

Pure functions (`combine_boundaries`, `build_segments`, `moments_from_segments`, the `parse_*` helpers) are separate from
ffmpeg I/O (`detect_scenes`, `detect_audio`) so they can be unit-tested with canned data. Detection output is cached per
asset (content_version + segmenter version + detection settings); combining boundaries into segments is cheap and
re-done on every run, so changing `segment_window_seconds` never re-runs ffmpeg.

Segment bounds are frame-rounded using the asset's rational fps. `segment_id` = `seg_` + 16 hex of
sha256("asset_id:start_frame:end_frame"). Every segment becomes one candidate moment (`moment_id` derived from it).
Note: after merging short neighbours a segment can exceed the window by less than `min_segment_seconds`.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from trip_vlog.logging_utils import Status
from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path
from trip_vlog.planning.duration import fps_string, frames_to_float, seconds_to_frames
from trip_vlog.state import StageResult, stable_hash

SEGMENTER_VERSION = 1
DEFAULT_FPS = Fraction(30)
AUDIO_ONLY_TIMEBASE = Fraction(1000)  # audio-only assets use millisecond "frames"
FLOOR_DB = -120.0
CLIP_PEAK_DB = -0.1
CLIP_WINDOW_FRACTION = 0.02


@dataclass(frozen=True)
class SegmentSettings:
    window_seconds: float = 6.0
    min_segment_seconds: float = 1.5
    scene_threshold: float = 0.30
    silence_db: float = -35.0
    min_silence_seconds: float = 0.5
    stats_window_seconds: float = 0.25

    @classmethod
    def from_config(cls, media: Any) -> "SegmentSettings":
        """Use optional `media.*` fields when the config defines them (see report), else module defaults."""
        d = cls()
        return cls(
            window_seconds=float(getattr(media, "segment_window_seconds", d.window_seconds)),
            min_segment_seconds=float(getattr(media, "segment_min_seconds", d.min_segment_seconds)),
            scene_threshold=float(getattr(media, "scene_threshold", d.scene_threshold)),
            silence_db=float(getattr(media, "silence_threshold_db", d.silence_db)),
        )

    def detect_key(self) -> str:
        """Settings that change what ffmpeg detects (not how detections are combined)."""
        return stable_hash({"v": SEGMENTER_VERSION, "scene": self.scene_threshold, "silence_db": self.silence_db,
                            "min_silence": self.min_silence_seconds, "stats": self.stats_window_seconds})

    def key(self) -> str:
        return stable_hash({"detect": self.detect_key(), "window": self.window_seconds, "min": self.min_segment_seconds})


# ---- ids ---------------------------------------------------------------------------------------

def segment_id_for(asset_id: str, start_frame: int | None, end_frame: int | None) -> str:
    """Stable ID from asset_id and frame-rounded bounds. Stills (no bounds) use the literal 'still'."""
    token = "still" if start_frame is None or end_frame is None else f"{start_frame}:{end_frame}"
    return "seg_" + hashlib.sha256(f"{asset_id}:{token}".encode("utf-8")).hexdigest()[:16]


def moment_id_for(segment_id: str) -> str:
    return "moment_" + segment_id.removeprefix("seg_")


# ---- pure: boundaries -> segments -----------------------------------------------------------------

@dataclass(frozen=True)
class Bounds:
    start_frame: int
    end_frame: int  # exclusive
    cut_score: float = 0.0  # scene-cut score of the boundary at start_frame (0 if none)

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


def combine_boundaries(total_frames: int, fps: Fraction, cuts: Sequence[tuple[float, float]],
                       silences: Sequence[tuple[float, float]], window_seconds: float, min_seconds: float) -> list[Bounds]:
    """Scene cuts + silence edges -> frame-rounded segments, split to the max window, short ones merged.

    1. Boundaries: each scene cut (time, score) and each silence start/end, rounded to frames, inside (0, total).
    2. Pieces longer than the window are split evenly (integer arithmetic) into ceil(len/window) parts.
    3. Pieces shorter than `min_seconds` are merged into the neighbour giving the shorter result (ties: the weaker
       cut, then the previous one), shortest piece first. A lone short asset stays one segment.
    """
    if total_frames < 1:
        return []
    marks: dict[int, float] = {}
    for t, score in cuts:
        f = seconds_to_frames(t, fps)
        if 0 < f < total_frames:
            marks[f] = max(marks.get(f, 0.0), float(score))
    for s, e in silences:
        for t in (s, e):
            f = seconds_to_frames(t, fps)
            if 0 < f < total_frames:
                marks.setdefault(f, 0.0)
    edges = [0, *sorted(marks), total_frames]
    window = max(1, seconds_to_frames(window_seconds, fps, "floor"))
    minimum = max(1, seconds_to_frames(min_seconds, fps))
    segs: list[Bounds] = []
    for a, b in zip(edges, edges[1:]):
        n = max(1, math.ceil((b - a) / window))
        pts = [a + ((b - a) * i) // n for i in range(n)] + [b]
        for i in range(n):
            segs.append(Bounds(pts[i], pts[i + 1], marks.get(a, 0.0) if i == 0 else 0.0))
    while len(segs) > 1:
        short = [(s.length, i) for i, s in enumerate(segs) if s.length < minimum]
        if not short:
            break
        _, i = min(short)
        prev_len = segs[i - 1].length + segs[i].length if i > 0 else None
        next_len = segs[i].length + segs[i + 1].length if i + 1 < len(segs) else None
        if next_len is None or (prev_len is not None and (prev_len, segs[i].cut_score, 0) <= (next_len, segs[i + 1].cut_score, 0)):
            j = i - 1  # merge into previous (cut at segs[i] disappears)
            segs[j] = Bounds(segs[j].start_frame, segs[i].end_frame, segs[j].cut_score)
            del segs[i]
        else:
            segs[i] = Bounds(segs[i].start_frame, segs[i + 1].end_frame, segs[i].cut_score)
            del segs[i + 1]
    return segs


def _db(x: float | None) -> float:
    if x is None or not math.isfinite(x):
        return FLOOR_DB
    return max(FLOOR_DB, x)


def audio_features(windows: Sequence[Sequence[float]], start_s: float, end_s: float, silence_db: float) -> dict[str, Any]:
    """Mean (power-averaged) and peak level within [start_s, end_s) from [t, rms_db, peak_db] windows."""
    inside = [w for w in windows if start_s - 1e-9 <= w[0] < end_s - 1e-9]
    if not inside:
        return {"audio_mean_db": None, "audio_peak_db": None, "silent": None, "clipped_audio_hint": None}
    power = sum(10 ** (_db(w[1]) / 10) for w in inside) / len(inside)
    mean_db = round(max(FLOOR_DB, 10 * math.log10(power)) if power > 0 else FLOOR_DB, 2)
    peak_db = round(max(_db(w[2]) for w in inside), 2)
    clipped = sum(1 for w in inside if _db(w[2]) >= CLIP_PEAK_DB) / len(inside) >= CLIP_WINDOW_FRACTION
    return {"audio_mean_db": mean_db, "audio_peak_db": peak_db, "silent": mean_db < silence_db, "clipped_audio_hint": clipped}


def build_segments(asset_id: str, media_type: str, duration_seconds: float | None, fps: Fraction | None,
                   detection: dict[str, Any] | None, settings: SegmentSettings) -> list[dict[str, Any]]:
    """Segment dicts (catalog `segments.data_json` shape) for one asset. Pure and deterministic.

    `detection` is the cached ffmpeg output {cuts, silences, windows, has_audio} or None when detection was unavailable
    (window-only segmentation, no features).
    """
    det = detection or {}
    if media_type == "image":
        sid = segment_id_for(asset_id, None, None)
        return [{"segment_id": sid, "asset_id": asset_id, "media_type": "image", "start_seconds": None, "end_seconds": None,
                 "start_frame": None, "end_frame": None, "fps": None, "features": {"still": True}, "segmenter_version": SEGMENTER_VERSION}]
    if duration_seconds is None or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return []
    rate = AUDIO_ONLY_TIMEBASE if media_type == "audio" else (fps or DEFAULT_FPS)
    total = seconds_to_frames(duration_seconds, rate, "floor")
    bounds = combine_boundaries(total, rate, det.get("cuts", []), det.get("silences", []) if det else [],
                                settings.window_seconds, settings.min_segment_seconds)
    out = []
    for b in bounds:
        sid = segment_id_for(asset_id, b.start_frame, b.end_frame)
        s_sec, e_sec = frames_to_float(b.start_frame, rate), frames_to_float(b.end_frame, rate)
        feats: dict[str, Any] = {"scene_cut_score": round(b.cut_score, 4), "has_audio": bool(det.get("has_audio", False))}
        if detection is None:
            feats.update(audio_mean_db=None, audio_peak_db=None, silent=None, clipped_audio_hint=None)
        elif det.get("has_audio"):
            feats.update(audio_features(det.get("windows", []), s_sec, e_sec, settings.silence_db))
        else:
            feats.update(audio_mean_db=None, audio_peak_db=None, silent=True, clipped_audio_hint=False)
        out.append({"segment_id": sid, "asset_id": asset_id, "media_type": media_type, "start_seconds": s_sec, "end_seconds": e_sec,
                    "start_frame": b.start_frame, "end_frame": b.end_frame, "fps": fps_string(rate), "features": feats,
                    "segmenter_version": SEGMENTER_VERSION})
    return out


def moment_tags(seg: dict[str, Any]) -> list[str]:
    f = seg.get("features", {})
    tags = [seg["media_type"]]
    if f.get("still"):
        tags.append("still")
    if f.get("silent") is True:
        tags.append("silent")
    elif f.get("silent") is False:
        tags.append("has_sound")
    if f.get("clipped_audio_hint"):
        tags.append("clipped_audio")
    if f.get("scene_cut_score", 0) > 0:
        tags.append("scene_cut_start")
    return tags


def moments_from_segments(segments: Sequence[dict[str, Any]], content_version: str | None) -> list[dict[str, Any]]:
    """Phase 2 baseline: each segment is one candidate moment (no description; tags from features). DESIGN §6.3 shape."""
    out = []
    for s in segments:
        audio = (s.get("features") or {}).get("audio")  # Phase 3c measurements, present once the `audio` stage ran
        out.append({
            "moment_id": moment_id_for(s["segment_id"]), "asset_id": s["asset_id"], "segment_id": s["segment_id"],
            "media_type": s["media_type"], "source_in_seconds": s["start_seconds"], "source_out_seconds": s["end_seconds"],
            "description": None, "visible_entities": [], "event": None, "speech_summary": None, "transcript_refs": [],
            "quality": {"sharpness": None, "stability": None, "exposure": None},
            "editorial_tags": moment_tags(s), "confidence": None, "evidence": [],
            "features": s.get("features", {}), "content_version": content_version, "source": "baseline",
            **({"audio": audio, "audio_tags": list(audio.get("audio_tags", []))} if audio else {}),
        })
    return out


# ---- pure: ffmpeg output parsers ---------------------------------------------------------------------

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.eE+-]+)")
_KV_RE = re.compile(r"lavfi\.astats\.Overall\.(RMS_level|Peak_level)=(\S+)")
_SIL_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def parse_scene_output(text: str) -> list[tuple[float, float]]:
    """`metadata=print` output of select='gt(scene,T)' -> [(time, score)]."""
    cuts: list[tuple[float, float]] = []
    pending: float | None = None
    for line in text.splitlines():
        m = _PTS_RE.search(line)
        if m:
            pending = float(m[1])
            continue
        m = _SCORE_RE.search(line)
        if m and pending is not None:
            cuts.append((round(pending, 4), round(float(m[1]), 4)))
            pending = None
    return sorted(cuts)


def parse_astats_output(text: str) -> list[list[float]]:
    """`ametadata=print` output of astats (reset per window) -> [[t, rms_db, peak_db], ...] (-inf clamped to -120)."""
    cur: dict[str, float] = {}
    t: float | None = None
    out: list[list[float]] = []

    def flush() -> None:
        if t is not None and cur:
            out.append([round(t, 4), round(_db(cur.get("RMS_level")), 2), round(_db(cur.get("Peak_level")), 2)])

    for line in text.splitlines():
        m = _PTS_RE.search(line)
        if m:
            flush()
            t, cur = float(m[1]), {}
            continue
        m = _KV_RE.search(line)
        if m:
            try:
                cur[m[1]] = float(m[2])
            except ValueError:
                cur[m[1]] = float("-inf")
    flush()
    return out


def parse_silence_output(text: str, duration: float | None) -> list[tuple[float, float]]:
    """silencedetect log lines -> [(start, end)]; an unterminated silence runs to `duration` (when known)."""
    out: list[tuple[float, float]] = []
    start: float | None = None
    for line in text.splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m[1]))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            out.append((round(start, 4), round(float(m[1]), 4)))
            start = None
    if start is not None and duration is not None and duration > start:
        out.append((round(start, 4), round(duration, 4)))
    return out


# ---- ffmpeg I/O ---------------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    cp = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace",
                        stdin=subprocess.DEVNULL, **popen_flags())
    return cp.returncode, cp.stdout or "", cp.stderr or ""


def detect_scenes(ffmpeg: Path, src: Path, threshold: float, timeout: float) -> list[tuple[float, float]]:
    """Scene cuts via select='gt(scene,T)' on a downscaled copy. Raises RuntimeError on ffmpeg failure."""
    vf = f"scale=160:-2:flags=fast_bilinear,select='gt(scene,{threshold:g})',metadata=mode=print:file=-"
    cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-nostdin", "-loglevel", "error", "-i", str(long_path(src.resolve())),
           "-an", "-sn", "-vf", vf, "-f", "null", "-"]
    try:
        rc, out, err = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"scene detection timed out after {timeout:g}s") from None
    except OSError as e:
        raise RuntimeError(f"cannot run ffmpeg: {e.strerror or e}") from None
    if rc != 0:
        raise RuntimeError(f"scene detection failed: {(err.strip().splitlines() or ['no output'])[-1]}")
    return parse_scene_output(out)


def detect_audio(ffmpeg: Path, src: Path, settings: SegmentSettings, duration: float | None,
                 timeout: float) -> tuple[list[list[float]], list[tuple[float, float]]]:
    """One audio pass: per-window RMS/peak (astats) and silence intervals (silencedetect). Raises RuntimeError."""
    n = max(1, round(16000 * settings.stats_window_seconds))
    base = f"aresample=16000,silencedetect=n={settings.silence_db:g}dB:d={settings.min_silence_seconds:g},asetnsamples=n={n}:p=0,"
    variants = [base + "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level+Peak_level,ametadata=mode=print:file=-",
                base + "astats=metadata=1:reset=1,ametadata=mode=print:file=-"]  # older ffmpeg without measure_* options
    last = "no output"
    for af in variants:
        cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info", "-i", str(long_path(src.resolve())),
               "-vn", "-sn", "-af", af, "-f", "null", "-"]
        try:
            rc, out, err = _run(cmd, timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"audio analysis timed out after {timeout:g}s") from None
        except OSError as e:
            raise RuntimeError(f"cannot run ffmpeg: {e.strerror or e}") from None
        if rc == 0:
            return parse_astats_output(out), parse_silence_output(err, duration)
        last = (err.strip().splitlines() or [last])[-1]
    raise RuntimeError(f"audio analysis failed: {last}")


def detect_asset(ffmpeg: Path, src: Path, media_type: str, audio_streams: int, duration: float | None,
                 settings: SegmentSettings) -> dict[str, Any]:
    """Run the ffmpeg detections needed for an asset. Returns the cacheable detection dict; raises RuntimeError."""
    timeout = max(120.0, 3.0 * (duration or 0.0))
    cuts: list[tuple[float, float]] = []
    windows: list[list[float]] = []
    silences: list[tuple[float, float]] = []
    has_audio = media_type == "audio" or audio_streams > 0
    if media_type == "video":
        cuts = detect_scenes(ffmpeg, src, settings.scene_threshold, timeout)
    if has_audio:
        windows, silences = detect_audio(ffmpeg, src, settings, duration, timeout)
    return {"cuts": [list(c) for c in cuts], "silences": [list(s) for s in silences], "windows": windows,
            "has_audio": has_audio, "stats_window_seconds": settings.stats_window_seconds}


# ---- stage ----------------------------------------------------------------------------------------------

def _asset_fps(row: Any) -> Fraction | None:
    num, den = row["frame_rate_num"], row["frame_rate_den"]
    return Fraction(num, den) if num and den else None


def segment_inputs(ctx: Any) -> Any:
    """Stage input description for StageRunner cache decisions."""
    s = SegmentSettings.from_config(ctx.config.media)
    return {"assets": [(r["asset_id"], r["content_version"], r["probe_status"], r["media_type"], r["duration_seconds"])
                       for r in ctx.catalog.list_assets()],
            "settings": s.key(), "ffmpeg": ctx.ffmpeg is not None, "v": SEGMENTER_VERSION}


def segment_assets(ctx: Any) -> StageResult:
    """Stage: segment every probed asset, persist segments + baseline moments, export media_index/moments.jsonl.

    `ctx` needs: config, catalog, paths.media_root, workspace.media_index, logger, ffmpeg (RunContext fits).
    """
    settings = SegmentSettings.from_config(ctx.config.media)
    cat = ctx.catalog
    detect_key = settings.detect_key()
    done = hits = failed = skipped = n_segments = 0
    warnings: list[str] = []
    if ctx.ffmpeg is None:
        warnings.append("ffmpeg not found: videos/audio segmented by window only, without scene or audio features")
    for row in cat.list_assets():
        aid, cv, mtype = row["asset_id"], row["content_version"], row["media_type"]
        if row["probe_status"] != "ok" or mtype not in ("video", "image", "audio"):
            skipped += 1
            ctx.logger.event("segment", Status.SKIPPED, asset_id=aid, message=f"not segmentable ({mtype or 'unprobed'})")
            continue
        t0 = time.perf_counter()
        duration = row["duration_seconds"]
        if mtype != "image" and not duration:
            failed += 1
            ctx.logger.event("segment", Status.FAILED, asset_id=aid, error="no duration in catalog", path=row["relative_path"])
            continue
        detection: dict[str, Any] | None = None
        cache = "miss"
        if mtype != "image":
            detection = cat.cache_get(aid, "segment_detect", detect_key, cv)
            if detection is not None:
                cache, hits = "hit", hits + 1
            elif ctx.ffmpeg is not None:
                src = ctx.paths.media_root / Path(*row["relative_path"].split("/"))
                try:
                    detection = detect_asset(ctx.ffmpeg, src, mtype, int(row["audio_streams"] or 0), duration, settings)
                    cat.cache_put(aid, "segment_detect", detect_key, cv, detection)
                except RuntimeError as e:
                    failed += 1
                    ctx.logger.event("segment", Status.FAILED, asset_id=aid, cache="miss", error=str(e), path=row["relative_path"])
                    detection = None  # fall back to window-only segmentation for this asset
        segs = build_segments(aid, mtype, duration, _asset_fps(row), detection, settings)
        if mtype == "video" and _asset_fps(row) is None:
            for s in segs:
                s["fps_assumed"] = True
        cat.replace_segments(aid, cv, segs)
        cat.replace_moments(aid, cv, moments_from_segments(segs, cv))
        n_segments += len(segs)
        done += 1
        ctx.logger.event("segment", Status.SUCCEEDED, asset_id=aid, cache=cache, duration_ms=(time.perf_counter() - t0) * 1000,
                         segments=len(segs))
    moments = cat.export_moments_jsonl(Path(ctx.workspace.media_index) / "moments.jsonl")
    summary: dict[str, Any] = {"assets_segmented": done, "segments": n_segments, "moments_exported": moments,
                               "detection_cache_hits": hits, "failed": failed, "skipped": skipped}
    if warnings:
        summary["warnings"] = warnings
    return StageResult(Status.PARTIAL if (failed or warnings) else Status.SUCCEEDED, summary)
