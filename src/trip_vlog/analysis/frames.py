"""Frame sampling for vision labeling (DESIGN §7.2 step 3) and per-segment contact sheets.

Sampling is a pure function of the segment bounds and the settings (integer frame arithmetic, no float drift): a segment of
N source frames is cut into `n = ceil(seconds * fps)` equal slices and the frame at the centre of each slice is sampled
(the first frame of a segment is often a cut transition, so slice edges are avoided). More than `max_frames` samples are
thinned evenly, always keeping the first and last. One ffmpeg process per segment decodes every frame (one accurate input
seek per frame). Files are named from the content version and the sampling settings, so a re-run reuses them and any
change produces new names (stale ones are removed).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from trip_vlog.analysis.labels import segment_fps
from trip_vlog.media import thumbnails as thumbs
from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path
from trip_vlog.planning.duration import frames_to_float
from trip_vlog.state import atomic_write_json, stable_hash

FRAMES_VERSION = 1
SHEET_TILE_W = 320
FRAMES_REL = "media_index/analysis/frames"
SHEETS_REL = "media_index/analysis/sheets"


@dataclass(frozen=True)
class SamplingSettings:
    fps: float = 1.0
    max_frames: int = 8
    max_side: int = 1568
    quality: int = 85

    @classmethod
    def from_config(cls, config: Any, max_images_per_request: int | None = None) -> "SamplingSettings":
        m = config.media
        cap = m.vision_max_frames_per_segment
        if max_images_per_request is not None:
            cap = min(cap, max_images_per_request)  # a segment's frames always travel together in one request
        return cls(fps=float(m.vision_sampling_fps), max_frames=int(cap), max_side=int(config.llm.image_max_side),
                   quality=int(config.llm.image_quality))

    def key(self) -> str:
        return stable_hash({"v": FRAMES_VERSION, "fps": self.fps, "max": self.max_frames, "side": self.max_side, "q": self.quality})


def sample_frame_numbers(start_frame: int, end_frame: int, fps: Fraction, sampling_fps: float, max_frames: int) -> list[int]:
    """Source frame numbers to sample inside [start_frame, end_frame). Always at least one, strictly increasing."""
    total = end_frame - start_frame
    if total < 1:
        return []
    seconds = float(Fraction(total) / fps)
    n = max(1, math.ceil(seconds * sampling_fps - 1e-6))
    ks = list(range(n))
    if n > max_frames:
        ks = [n // 2] if max_frames == 1 else sorted({round(j * (n - 1) / (max_frames - 1)) for j in range(max_frames)})
    out: list[int] = []
    for k in ks:
        f = start_frame + (2 * k + 1) * total // (2 * n)
        f = min(max(f, start_frame), end_frame - 1)
        if not out or f > out[-1]:
            out.append(f)
    return out


def _short(*parts: Any) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


def frame_records(seg: dict[str, Any], content_version: str | None, settings: SamplingSettings) -> list[dict[str, Any]]:
    """Planned frames of a video segment: index, source frame number, exact timestamp and workspace-relative artifact path."""
    fps = segment_fps(seg)
    nums = sample_frame_numbers(int(seg["start_frame"]), int(seg["end_frame"]), fps, settings.fps, settings.max_frames)
    tag = _short(content_version, settings.key())
    return [{"index": i, "frame": f, "timestamp_seconds": frames_to_float(f, fps),
             "artifact": f"{FRAMES_REL}/{seg['asset_id']}/{seg['segment_id']}_{tag}_{i:02d}.jpg"} for i, f in enumerate(nums)]


def sheet_rel(seg: dict[str, Any], content_version: str | None, settings: SamplingSettings) -> str:
    return f"{SHEETS_REL}/{seg['asset_id']}/{seg['segment_id']}_{_short(content_version, settings.key(), SHEET_TILE_W)}.jpg"


def frames_present(root: Path, records: Sequence[dict[str, Any]]) -> bool:
    return bool(records) and all((root / r["artifact"]).is_file() and (root / r["artifact"]).stat().st_size > 0 for r in records)


def _cleanup_stale(root: Path, records: Sequence[dict[str, Any]], seg: dict[str, Any]) -> None:
    keep = {Path(r["artifact"]).name for r in records} | {Path(records[0]["artifact"].rsplit("_", 1)[0] + ".frames.json").name}
    d = (root / records[0]["artifact"]).parent
    for pattern in (f"{seg['segment_id']}_*.jpg", f"{seg['segment_id']}_*.frames.json"):
        for p in d.glob(pattern):
            if p.name not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass


def _extract(ffmpeg: Path, src: Path, items: Sequence[tuple[float, Path]], settings: SamplingSettings, timeout: float) -> str | None:
    """One ffmpeg process, one accurate input seek per frame, one JPEG per frame. Returns an error string or None."""
    vf = (f"scale='min({settings.max_side},iw)':'min({settings.max_side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2")
    q = max(2, min(31, round(31 - settings.quality * 0.29)))
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for t, _ in items:
        cmd += ["-ss", f"{t:.6f}", "-i", str(long_path(src.resolve()))]
    for i, (_, out) in enumerate(items):
        cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-an", "-sn", "-vf", vf, "-q:v", str(q), "-f", "image2", str(out)]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, **popen_flags())
    except subprocess.TimeoutExpired:
        return f"ffmpeg timed out after {timeout:g}s"
    except OSError as e:
        return f"cannot run ffmpeg: {e.strerror or e}"
    if cp.returncode != 0:
        msg = (cp.stderr or "").strip().splitlines()
        return f"ffmpeg failed: {msg[-1] if msg else 'no output'}"
    return None


def extract_segment_frames(ffmpeg: Path | None, src: Path, seg: dict[str, Any], records: Sequence[dict[str, Any]], root: Path,
                           settings: SamplingSettings) -> str | None:
    """Make every planned frame exist under `root` (skipped when they already do). Returns an error string or None."""
    if frames_present(root, records):
        return None
    if ffmpeg is None:
        return "ffmpeg not found (install FFmpeg or set media.ffmpeg_path / $FFMPEG)"
    if not records:
        return "segment has no frames to sample"
    fps = segment_fps(seg)
    half = Fraction(1, 2) / fps  # seek to the middle of the frame so float rounding cannot land on the previous one
    targets = [(root / r["artifact"], float(Fraction(r["frame"]) / fps + half)) for r in records]
    tmps = [out.with_name(f".{out.stem}.{os.getpid()}.tmp.jpg") for out, _ in targets]
    timeout = 60.0 + 15.0 * len(records)
    try:
        targets[0][0].parent.mkdir(parents=True, exist_ok=True)
        err = _extract(ffmpeg, src, [(t, tmp) for (_, t), tmp in zip(targets, tmps)], settings, timeout)
        if err is None:
            missing = [i for i, tmp in enumerate(tmps) if not tmp.is_file() or tmp.stat().st_size == 0]
            for i in missing:  # one retry per missing frame, a quarter frame later
                err = _extract(ffmpeg, src, [(targets[i][1] + float(half) / 2, tmps[i])], settings, 60.0)
                if err or not tmps[i].is_file() or tmps[i].stat().st_size == 0:
                    return f"frame {i} (t={records[i]['timestamp_seconds']:.3f}s) could not be decoded" + (f": {err}" if err else "")
        else:
            return err
        for (out, _), tmp in zip(targets, tmps):
            os.replace(tmp, out)
        atomic_write_json(root / (records[0]["artifact"].rsplit("_", 1)[0] + ".frames.json"),
                          {"segment_id": seg["segment_id"], "frames": list(records)})
        _cleanup_stale(root, records, seg)
        return None
    finally:
        for tmp in tmps:
            try:
                tmp.unlink()
            except OSError:
                pass


def make_segment_sheet(records: Sequence[dict[str, Any]], seg: dict[str, Any], root: Path, out_rel: str) -> str | None:
    """Grid of the sampled frames, each tile labelled with the segment ID, frame index and timestamp. Reuses the thumbnail helpers."""
    out = root / out_rel
    if out.is_file() and out.stat().st_size > 0:
        return None
    try:
        tiles = [thumbs._label_tile(root / r["artifact"], [seg["segment_id"], f"#{r['index']}  t={r['timestamp_seconds']:.2f}s"], SHEET_TILE_W)
                 for r in records]
        if not tiles:
            return "no frames for the contact sheet"
        thumbs._compose(tiles, min(4, len(tiles)), SHEET_TILE_W, out)
    except Exception as e:  # noqa: BLE001 - the sheet is an artifact; a failure is reported, not fatal
        return f"cannot create contact sheet: {type(e).__name__}: {e}"
    for p in out.parent.glob(f"{seg['segment_id']}_*.jpg"):
        if p.name != out.name:
            try:
                p.unlink()
            except OSError:
                pass
    return None
