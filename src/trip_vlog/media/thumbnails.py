"""Thumbnails and contact sheets, written only under the workspace (never next to originals)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path, safe_filename

THUMB_VERSION = 1
SHEET_VERSION = 1
SHEET_PAGE_SIZE = 20
TILE_W = 200


@dataclass(frozen=True)
class ThumbSettings:
    max_side: int = 480
    quality: int = 75
    version: int = THUMB_VERSION

    def key(self) -> str:
        return hashlib.sha256(f"{self.max_side}:{self.quality}:{self.version}".encode()).hexdigest()[:8]


def _short(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def thumbnail_name(asset_id: str, content_version: str | None, settings: ThumbSettings) -> str:
    return f"{asset_id}_{_short(content_version or 'none')}_{settings.key()}.jpg"


def midpoint_seconds(duration: float | None) -> float:
    return max(0.0, (duration or 0.0) / 2.0)


def _remove_stale(directory: Path, prefix: str, keep: str) -> None:
    for p in directory.glob(prefix + "*.jpg"):
        if p.name != keep:
            try:
                p.unlink()
            except OSError:
                pass


def _run_ffmpeg_frame(ffmpeg: Path, src: Path, seconds: float, out: Path, max_side: int, quality: int, timeout: float = 60.0) -> str | None:
    """Extract one frame (autorotated by ffmpeg) to `out` atomically. Returns an error string or None."""
    tmp = out.with_name(f".{out.stem}.{os.getpid()}.tmp.jpg")
    vf = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    q = max(2, min(31, round(31 - quality * 0.29)))  # 1..100 quality -> mjpeg qscale
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(long_path(src.resolve())),
           "-frames:v", "1", "-an", "-sn", "-vf", vf, "-q:v", str(q), str(tmp)]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, **popen_flags())
    except subprocess.TimeoutExpired:
        return f"ffmpeg timed out after {timeout:g}s"
    except OSError as e:
        return f"cannot run ffmpeg: {e.strerror or e}"
    try:
        if cp.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            msg = (cp.stderr or "").strip().splitlines()
            return f"ffmpeg failed: {msg[-1] if msg else 'no frame produced'}"
        os.replace(tmp, out)
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def make_video_thumbnail(ffmpeg: Path | None, src: Path, duration: float | None, out: Path, settings: ThumbSettings) -> str | None:
    if ffmpeg is None:
        return "ffmpeg not found (install FFmpeg or set media.ffmpeg_path / $FFMPEG)"
    out.parent.mkdir(parents=True, exist_ok=True)
    err = _run_ffmpeg_frame(ffmpeg, src, midpoint_seconds(duration), out, settings.max_side, settings.quality)
    if err and midpoint_seconds(duration) > 0:
        err = _run_ffmpeg_frame(ffmpeg, src, 0.0, out, settings.max_side, settings.quality)
    return err


def make_image_thumbnail(src: Path, out: Path, settings: ThumbSettings) -> str | None:
    from PIL import Image, ImageOps

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.{os.getpid()}.tmp.jpg")
    try:
        with Image.open(long_path(src)) as im:
            im.draft("RGB", (settings.max_side * 2, settings.max_side * 2))
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGBA") if "A" in im.getbands() else im.convert("RGB")
            if im.mode == "RGBA":
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im.thumbnail((settings.max_side, settings.max_side))
            im.save(tmp, "JPEG", quality=settings.quality)
        os.replace(tmp, out)
        return None
    except Exception as e:  # noqa: BLE001
        return f"cannot create thumbnail: {type(e).__name__}: {e}"
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _font(size: int = 11):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 (scalable built-in font)
    except TypeError:
        return ImageFont.load_default()


def _label_tile(path: Path, lines: Sequence[str], tile_w: int):
    """Return an RGB tile: image scaled to tile_w wide plus a label strip underneath."""
    from PIL import Image, ImageDraw

    with Image.open(path) as im:
        im = im.convert("RGB")
        h = max(1, round(im.height * tile_w / im.width))
        im = im.resize((tile_w, h))
    font = _font(11)
    line_h = 14
    tile = Image.new("RGB", (tile_w, h + line_h * len(lines) + 4), (24, 24, 24))
    tile.paste(im, (0, 0))
    d = ImageDraw.Draw(tile)
    for i, line in enumerate(lines):
        d.text((3, h + 2 + i * line_h), line, fill=(235, 235, 235), font=font)
    return tile


def _compose(tiles, cols: int, tile_w: int, out: Path, gap: int = 6) -> None:
    from PIL import Image

    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    row_h = [max(t.height for t in r) for r in rows]
    sheet = Image.new("RGB", (cols * tile_w + (cols + 1) * gap, sum(row_h) + (len(rows) + 1) * gap), (0, 0, 0))
    y = gap
    for r, rh in zip(rows, row_h):
        for c, t in enumerate(r):
            sheet.paste(t, (gap + c * (tile_w + gap), y))
        y += rh + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.{os.getpid()}.tmp.jpg")
    try:
        sheet.save(tmp, "JPEG", quality=80)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def fmt_ts(seconds: float) -> str:
    return f"t={seconds:.1f}s"


def asset_sheet_name(asset_id: str, content_version: str | None, frames: int, settings: ThumbSettings) -> str:
    return f"{asset_id}_{_short(f'{content_version}:{frames}:{SHEET_VERSION}')}_{settings.key()}.jpg"


def make_asset_contact_sheet(ffmpeg: Path | None, src: Path, asset_id: str, duration: float | None, frames: int,
                             out: Path, tmp_root: Path, settings: ThumbSettings) -> str | None:
    """Evenly spaced frames from one video, labeled with asset ID + timestamp."""
    if ffmpeg is None:
        return "ffmpeg not found"
    if not duration or duration <= 0 or frames < 1:
        return "duration unknown; no contact sheet"
    times = [duration * (i + 0.5) / frames for i in range(frames)]
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="sheet_") as td:
        tiles = []
        for i, t in enumerate(times):
            fp = Path(td) / f"f{i:02d}.jpg"
            err = _run_ffmpeg_frame(ffmpeg, src, t, fp, TILE_W * 2, settings.quality)
            if err:
                continue
            tiles.append(_label_tile(fp, [asset_id, fmt_ts(t)], TILE_W))
        if not tiles:
            return "no frames could be extracted"
        _compose(tiles, min(3, len(tiles)), TILE_W, out)
    _remove_stale(out.parent, f"{asset_id}_", out.name)
    return None


def folder_sheet_prefix(folder: str) -> str:
    return f"folder_{safe_filename(folder or 'root', 24)}_{_short(folder)}"


def make_folder_contact_sheets(items: Sequence[tuple[str, Path, str]], folder: str, sheets_dir: Path,
                               settings: ThumbSettings) -> tuple[list[Path], str | None]:
    """items: (asset_id, thumbnail_path, caption e.g. 't=2.5s' or 'photo'). Paginated by SHEET_PAGE_SIZE."""
    outs: list[Path] = []
    prefix = folder_sheet_prefix(folder)
    pages = [items[i:i + SHEET_PAGE_SIZE] for i in range(0, len(items), SHEET_PAGE_SIZE)]
    for n, page in enumerate(pages, 1):
        key = _short(":".join(f"{a}:{p.name}:{c}" for a, p, c in page) + settings.key() + str(SHEET_VERSION))
        out = sheets_dir / f"{prefix}_p{n:02d}_{key}.jpg"
        if not out.exists():
            try:
                tiles = [_label_tile(p, [a, c], TILE_W) for a, p, c in page]
                _compose(tiles, min(4, len(tiles)), TILE_W, out)
            except Exception as e:  # noqa: BLE001
                return outs, f"cannot create folder sheet: {type(e).__name__}: {e}"
        _remove_stale(sheets_dir, f"{prefix}_p{n:02d}_", out.name)
        outs.append(out)
    for p in sheets_dir.glob(prefix + "_p*.jpg"):  # pages that no longer exist
        if p not in outs:
            try:
                p.unlink()
            except OSError:
                pass
    return outs, None
