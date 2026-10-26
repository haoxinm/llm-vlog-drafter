"""Prepare local images for upload: orient, downscale, strip metadata, re-encode as JPEG (DESIGN §17).

Re-encoding drops EXIF/GPS and other metadata, so location data is never sent with a frame. The bytes
returned here are exactly the bytes sent to the provider. The request fingerprint does NOT hash them (JPEG encoders may differ
between OSes/Pillow builds); it hashes the source file bytes plus the preparation parameters instead.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from trip_vlog.llm.types import ImagePart, LLMInputError, PreparedImage

DEFAULT_MAX_SIDE = 1568
DEFAULT_QUALITY = 85
IMAGE_PREP_VERSION = "p1"  # bump when the encoding pipeline changes (part of the prepared-image cache key and the fingerprint)


def prepare_image(
    part: ImagePart,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    quality: int = DEFAULT_QUALITY,
    cache_dir: Path | None = None,
) -> PreparedImage:
    try:
        raw = part.path.read_bytes()
    except OSError as e:
        raise LLMInputError(f"cannot read image {part.path.name}: {e.strerror or e}") from None
    src_hash = hashlib.sha256(raw).hexdigest()
    cached = cache_dir / f"{src_hash}-{max_side}-{quality}-{IMAGE_PREP_VERSION}.jpg" if cache_dir else None
    if cached is not None:
        try:
            data = cached.read_bytes()
            return _prepared(data, src_hash, max_side, quality, part.identity)
        except OSError:
            pass
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            else:
                im = im.convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)  # no exif= argument: metadata is dropped
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as e:
        raise LLMInputError(f"cannot decode image {part.path.name}: {type(e).__name__}") from None
    data = buf.getvalue()
    if cached is not None:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_name(f".{cached.name}.{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cached)
        except OSError:
            pass  # the cache is an optimisation only
    return _prepared(data, src_hash, max_side, quality, part.identity)


def _prepared(data: bytes, src_hash: str, max_side: int, quality: int, identity: str | None = None) -> PreparedImage:
    return PreparedImage("image/jpeg", data, hashlib.sha256(data).hexdigest(), src_hash, max_side, quality, IMAGE_PREP_VERSION, identity)
