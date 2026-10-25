"""Contact sheet of the rendered preview around each cut (DESIGN §14 steps 4-5): ffmpeg frame grabs + Pillow."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from trip_vlog.media.tools import popen_flags

CUT_OFFSET_SECONDS = 0.15


@dataclass
class SheetResult:
    path: Path | None
    frames: int
    failed: list[str]
    error: str | None = None


def sample_times(cut_seconds: list[float], duration: float, max_frames: int) -> list[tuple[float, str]]:
    """(timestamp, label) pairs: just before and just after every cut, plus first and last frame; capped at max_frames."""
    last = max(0.0, duration - 0.05)
    clamp = lambda t: min(max(t, 0.0), last)
    pts: list[tuple[float, str]] = [(0.0, "start")]
    for i, c in enumerate(cut_seconds, 1):
        pts.append((clamp(c - CUT_OFFSET_SECONDS), f"cut {i} before"))
        pts.append((clamp(c + CUT_OFFSET_SECONDS), f"cut {i} after"))
    pts.append((last, "end"))
    if len(pts) > max_frames:
        step = len(pts) / max_frames
        pts = [pts[int(i * step)] for i in range(max_frames)]
    return pts


def grab_frame(ffmpeg: Path, video: Path, t: float, out: Path, width: int, timeout: float = 60.0) -> str | None:
    cp = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                         "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)],
                        capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                        shell=False, **popen_flags())
    if cp.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        return (cp.stderr or "no frame produced").strip()[-200:]
    return None


def _ts(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:06.3f}"


def make_timeline_sheet(ffmpeg: Path, preview: Path, out: Path, cut_seconds: list[float], duration: float, *,
                        columns: int = 4, tile_width: int = 320, max_frames: int = 48) -> SheetResult:
    from PIL import Image, ImageDraw, ImageFont

    pts = sample_times(cut_seconds, duration, max_frames)
    failed: list[str] = []
    tiles: list[tuple[str, "Image.Image"]] = []
    with tempfile.TemporaryDirectory(prefix="tv-sheet-") as td:
        for i, (t, label) in enumerate(pts):
            f = Path(td) / f"f{i:03d}.jpg"
            err = grab_frame(ffmpeg, preview, t, f, tile_width)
            if err:
                failed.append(f"{label} @ {_ts(t)}: {err}")
                continue
            with Image.open(f) as im:
                tiles.append((f"{label}  {_ts(t)}", im.convert("RGB").copy()))
    if not tiles:
        return SheetResult(None, 0, failed, "no frames could be extracted from the preview")
    th = max(im.height for _, im in tiles)
    label_h, pad = 18, 6
    rows = -(-len(tiles) // columns)
    cols = min(columns, len(tiles))
    sheet = Image.new("RGB", (cols * (tile_width + pad) + pad, rows * (th + label_h + pad) + pad), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=12)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    for i, (label, im) in enumerate(tiles):
        x = pad + (i % columns) * (tile_width + pad)
        y = pad + (i // columns) * (th + label_h + pad)
        sheet.paste(im, (x, y + label_h))
        draw.text((x + 2, y + 2), label, fill=(235, 235, 235), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp.jpg")
    sheet.save(tmp, "JPEG", quality=85)
    tmp.replace(out)
    return SheetResult(out, len(tiles), failed)


def cut_times_from_starts(start_frames: list[int], fps: Fraction) -> list[float]:
    """Timeline seconds of every cut (clip starts after the first)."""
    return [float(Fraction(f) / fps) for f in sorted(set(start_frames)) if f > 0]
