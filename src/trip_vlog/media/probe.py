"""ffprobe-based metadata extraction (DESIGN §7.1, §6.2).

`parse_ffprobe_json` is pure (testable with canned JSON); `probe_file` runs ffprobe / Pillow.
Capture times keep their original offset when known and are never converted from unknown local time to UTC.
GPS is private: it is returned here but kept out of logs and (by default) out of JSONL exports.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path

PROBE_VERSION = 1
IMAGE_FORMATS = {"image2", "png_pipe", "jpeg_pipe", "webp_pipe", "tiff_pipe", "bmp_pipe", "gif", "heif", "avif"}
IMAGE_CODECS = {"mjpeg", "png", "bmp", "webp", "tiff", "gif", "hevc_still", "heic", "av1_still", "jpeg2000", "ppm", "pgm"}
VFR_TOLERANCE = Fraction(5, 1000)  # relative difference between r_frame_rate and avg_frame_rate


def parse_rational(value: Any) -> Fraction | None:
    """Parse ffprobe rationals like '30000/1001' or '25'. '0/0', '0/1' and garbage -> None."""
    if value is None:
        return None
    try:
        fr = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return fr if fr > 0 else None


@dataclass
class ProbeResult:
    status: str = "ok"  # ok | failed
    error: str | None = None
    media_type: str = "unknown"  # video | image | audio | unknown
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    display_width: int | None = None  # after rotation
    display_height: int | None = None
    frame_rate: Fraction | None = None
    is_vfr: bool = False
    rotation_degrees: int = 0  # clockwise, 0/90/180/270
    codec: str | None = None
    pix_fmt: str | None = None
    hdr: str | None = None  # "pq" | "hlg" | None
    audio_streams: int = 0
    capture_time: str | None = None
    capture_time_source: str | None = None
    gps: dict[str, Any] | None = None
    format_name: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["frame_rate"] = [self.frame_rate.numerator, self.frame_rate.denominator] if self.frame_rate else None
        d["notes"] = list(self.notes)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProbeResult":
        d = dict(d)
        fr = d.pop("frame_rate", None)
        r = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        r.frame_rate = Fraction(fr[0], fr[1]) if fr else None
        return r


# ---- capture time / GPS ---------------------------------------------------------------------

def parse_capture_time(raw: str | None) -> tuple[str, bool] | None:
    """Normalize a metadata timestamp to ISO 8601. Returns (iso, has_timezone) or None.

    Keeps an explicit offset ('-1000' -> '-10:00'; 'Z' -> '+00:00'). A timestamp without offset stays naive.
    """
    if not raw:
        return None
    s = str(raw).strip().replace("\x00", "")
    if not s or s.startswith("0000") or s.startswith("1970-01-01T00:00:00"):
        return None
    m = re.fullmatch(r"(\d{4})[-:](\d{2})[-:](\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?", s)
    if not m:
        return None
    y, mo, d, h, mi, se, frac, tz = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    except ValueError:
        return None
    iso = f"{y}-{mo}-{d}T{h}:{mi}:{se}"
    if tz:
        if tz == "Z":
            tz = "+00:00"
        elif ":" not in tz:
            tz = tz[:3] + ":" + tz[3:]
        return iso + tz, True
    return iso, False


def parse_iso6709(raw: str | None) -> dict[str, Any] | None:
    """'+37.7749-122.4194+012.300/' -> {latitude, longitude, altitude}."""
    if not raw:
        return None
    m = re.match(r"\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?", str(raw))
    if not m:
        return None
    lat, lon = float(m[1]), float(m[2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    out: dict[str, Any] = {"latitude": lat, "longitude": lon}
    if m[3] is not None:
        out["altitude"] = float(m[3])
    return out


def _tags(*containers: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in containers:
        for k, v in ((c or {}).get("tags") or {}).items():
            out.setdefault(str(k).lower(), str(v))
    return out


# ---- ffprobe JSON ---------------------------------------------------------------------------

def _rotation(video: dict[str, Any], tags: dict[str, str], notes: list[str]) -> int:
    rot: float | None = None
    for sd in video.get("side_data_list") or []:
        if str(sd.get("side_data_type", "")).lower().startswith("display matrix") and "rotation" in sd:
            try:
                rot = -float(sd["rotation"])  # ffprobe: counter-clockwise needed to display -> clockwise
            except (TypeError, ValueError):
                pass
    if rot is None and "rotate" in (video.get("tags") or {}):
        try:
            rot = float(video["tags"]["rotate"])  # legacy tag, clockwise
        except (TypeError, ValueError):
            pass
    if rot is None:
        return 0
    snapped = int(round(rot / 90.0)) * 90 % 360
    if abs(rot - round(rot / 90.0) * 90) > 1:
        notes.append(f"non-right-angle rotation {rot:g} degrees snapped to {snapped}")
    return snapped


def parse_ffprobe_json(data: dict[str, Any], hint_media_type: str | None = None) -> ProbeResult:
    """Convert ffprobe `-show_format -show_streams` JSON into a ProbeResult. Never raises on odd input."""
    r = ProbeResult()
    if not isinstance(data, dict):
        return ProbeResult(status="failed", error="ffprobe output is not a JSON object")
    if "error" in data and not data.get("streams"):
        err = data["error"]
        msg = err.get("string") if isinstance(err, dict) else str(err)
        return ProbeResult(status="failed", error=f"ffprobe error: {msg}")
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    if not streams:
        return ProbeResult(status="failed", error="no media streams found (file may be corrupt or unsupported)")
    r.format_name = fmt.get("format_name")
    videos = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    pics = [s for s in streams if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    r.audio_streams = len(audios)
    fmt_names = set((r.format_name or "").split(","))

    video = videos[0] if videos else None
    if hint_media_type in ("image", "video"):
        is_image = hint_media_type == "image"
    else:
        is_image = bool(video) and (bool(fmt_names & IMAGE_FORMATS) or (
            video.get("codec_name") in IMAGE_CODECS and not fmt.get("duration")))
    if hint_media_type == "image" and not video:
        video = pics[0] if pics else None
    if video is not None and is_image:
        r.media_type = "image"
    elif video is not None:
        r.media_type = "video"
    elif audios:
        r.media_type = "audio"
    else:
        return ProbeResult(status="failed", error="no usable video or audio stream", format_name=r.format_name)

    tags = _tags(fmt, video, *(audios[:1]))

    # duration
    dur = None
    for src in (fmt, video or {}, audios[0] if audios else {}):
        try:
            v = float(src.get("duration"))
            if v >= 0 and v == v and v != float("inf"):
                dur = v
                break
        except (TypeError, ValueError):
            continue
    r.duration_seconds = None if r.media_type == "image" else dur
    if r.media_type == "video" and (dur is None or dur <= 0):
        r.notes.append("duration unknown or zero")

    if video is not None:
        r.codec = video.get("codec_name")
        r.pix_fmt = video.get("pix_fmt")
        try:
            r.width, r.height = int(video["width"]), int(video["height"])
        except (KeyError, TypeError, ValueError):
            r.status, r.error = "failed", "video stream has no valid dimensions"
            return r
        r.rotation_degrees = _rotation(video, tags, r.notes)
        if r.rotation_degrees in (90, 270):
            r.display_width, r.display_height = r.height, r.width
        else:
            r.display_width, r.display_height = r.width, r.height
        if r.media_type == "video":
            rfr, afr = parse_rational(video.get("r_frame_rate")), parse_rational(video.get("avg_frame_rate"))
            if rfr and afr and abs(rfr - afr) / rfr > VFR_TOLERANCE:
                r.is_vfr = True
                r.notes.append(f"variable frame rate (r_frame_rate={rfr}, avg_frame_rate={float(afr):.3f})")
            r.frame_rate = afr or rfr
            if r.frame_rate and r.frame_rate >= 100:
                r.notes.append(f"high frame rate ({float(r.frame_rate):g} fps)")
        ct = str(video.get("color_transfer") or "")
        if ct == "smpte2084":
            r.hdr = "pq"
        elif ct == "arib-std-b67":
            r.hdr = "hlg"
        if r.hdr:
            r.notes.append(f"HDR ({'PQ/HDR10' if r.hdr == 'pq' else 'HLG'}); previews/thumbnails are not tone-mapped")
        if any("dolby vision" in str(sd.get("side_data_type", "")).lower() for sd in video.get("side_data_list") or []):
            r.notes.append("Dolby Vision metadata present")
        if r.rotation_degrees:
            r.notes.append(f"rotated {r.rotation_degrees} degrees; display size {r.display_width}x{r.display_height}")
    if r.media_type == "video" and not audios:
        r.notes.append("no audio stream")

    # capture time: QuickTime creationdate keeps the local offset, so prefer it over creation_time (UTC)
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date", "datetimeoriginal"):
        pt = parse_capture_time(tags.get(key))
        if pt:
            r.capture_time, r.capture_time_source = pt[0], key
            if not pt[1]:
                r.notes.append("capture time has no timezone offset (local time, not converted)")
            break
    for key in ("com.apple.quicktime.location.iso6709", "location", "location-eng"):
        g = parse_iso6709(tags.get(key))
        if g:
            r.gps = {**g, "source": key}
            break
    return r


# ---- running ffprobe ------------------------------------------------------------------------

def run_ffprobe(ffprobe: Path, path: Path, timeout: float = 60.0) -> dict[str, Any] | ProbeResult:
    """Run ffprobe; return parsed JSON dict, or a failed ProbeResult. Argument list, no shell."""
    cmd = [str(ffprobe), "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
           "-show_format", "-show_streams", "-show_error", "-i", str(long_path(path.resolve()))]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, **popen_flags())
    except subprocess.TimeoutExpired:
        return ProbeResult(status="failed", error=f"ffprobe timed out after {timeout:g}s")
    except OSError as e:
        return ProbeResult(status="failed", error=f"cannot run ffprobe: {e.strerror or e}")
    try:
        data = json.loads(cp.stdout) if cp.stdout.strip() else {}
    except ValueError:
        data = {}
    if cp.returncode != 0 or not data:
        msg = (cp.stderr or "").strip().splitlines()
        detail = msg[-1] if msg else (f"exit code {cp.returncode}")
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            detail = data["error"].get("string") or detail
        return ProbeResult(status="failed", error=f"ffprobe failed: {detail}")
    return data


_ORIENTATION = {  # EXIF orientation -> (clockwise degrees to display, mirrored)
    1: (0, False), 2: (0, True), 3: (180, False), 4: (180, True),
    5: (270, True), 6: (90, False), 7: (90, True), 8: (270, False),
}


def _exif_dt(exif_main: Any, exif_ifd: Any) -> tuple[str, bool, str] | None:
    for tag, src, name in ((0x9003, exif_ifd, "DateTimeOriginal"), (0x9004, exif_ifd, "DateTimeDigitized"),
                           (0x0132, exif_main, "DateTime")):
        raw = src.get(tag) if src else None
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "ignore")
        if not raw:
            continue
        off = (exif_ifd.get(0x9011) or exif_ifd.get(0x9010)) if exif_ifd else None
        if isinstance(off, bytes):
            off = off.decode("ascii", "ignore")
        text = str(raw).strip().replace("\x00", "")
        if off and re.fullmatch(r"[+-]\d{2}:\d{2}", str(off).strip()):
            text += str(off).strip()
        pt = parse_capture_time(text)
        if pt:
            return pt[0], pt[1], name
    return None


def _dms(vals: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(x) for x in vals)
        v = d + m / 60 + s / 3600
        if isinstance(ref, bytes):
            ref = ref.decode("ascii", "ignore")
        return -v if str(ref).upper().startswith(("S", "W")) else v
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_image(path: Path) -> ProbeResult:
    """Pillow-based image metadata (EXIF orientation, DateTimeOriginal + offset, GPS)."""
    from PIL import Image  # local import keeps `trip_vlog.media.probe` cheap to import

    try:
        with Image.open(long_path(path)) as im:
            im.verify()
        with Image.open(long_path(path)) as im:
            r = ProbeResult(media_type="image", width=im.width, height=im.height, codec=(im.format or "").lower() or None,
                            pix_fmt=im.mode, format_name=im.format)
            exif = im.getexif()
            exif_ifd = exif.get_ifd(0x8769) if exif else {}
            gps_ifd = exif.get_ifd(0x8825) if exif else {}
            orient = exif.get(0x0112) if exif else None
    except Exception as e:  # noqa: BLE001 - Pillow raises many types for bad files
        return ProbeResult(status="failed", media_type="image", error=f"cannot read image: {type(e).__name__}: {e}")
    rot, mirrored = _ORIENTATION.get(orient, (0, False)) if isinstance(orient, int) else (0, False)
    r.rotation_degrees = rot
    if mirrored:
        r.notes.append("EXIF orientation includes mirroring")
    swap = orient in (5, 6, 7, 8)
    r.display_width, r.display_height = (r.height, r.width) if swap else (r.width, r.height)
    if rot:
        r.notes.append(f"EXIF orientation {orient}: rotated {rot} degrees; display size {r.display_width}x{r.display_height}")
    dt = _exif_dt(exif, exif_ifd)
    if dt:
        r.capture_time, r.capture_time_source = dt[0], f"exif:{dt[2]}"
        if not dt[1]:
            r.notes.append("capture time has no timezone offset (local time, not converted)")
    lat, lon = _dms(gps_ifd.get(2), gps_ifd.get(1)) if gps_ifd.get(2) else None, None
    if gps_ifd.get(4):
        lon = _dms(gps_ifd.get(4), gps_ifd.get(3))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        r.gps = {"latitude": lat, "longitude": lon, "source": "exif"}
    return r


def probe_file(path: Path, hint_media_type: str, ffprobe: Path | None, timeout: float = 60.0) -> ProbeResult:
    """Probe one file. Never raises; failures come back as status='failed' with a message."""
    try:
        if hint_media_type == "image":
            res = probe_image(path)
            if res.status == "ok" or ffprobe is None or path.suffix.lower() not in (".heic", ".heif"):
                return res  # ffprobe fallback only for formats Pillow cannot decode (HEIC/HEIF)
            data = run_ffprobe(ffprobe, path, timeout)  # e.g. formats Pillow lacks
            if isinstance(data, ProbeResult):
                data.error = f"{res.error}; {data.error}"
                return data
            alt = parse_ffprobe_json(data, "image")
            return alt if alt.status == "ok" else res
        if ffprobe is None:
            return ProbeResult(status="failed", error="ffprobe not found (install FFmpeg or set media.ffprobe_path / $FFPROBE)")
        data = run_ffprobe(ffprobe, path, timeout)
        if isinstance(data, ProbeResult):
            return data
        return parse_ffprobe_json(data, hint_media_type)
    except Exception as e:  # noqa: BLE001 - one bad file must never abort the scan
        return ProbeResult(status="failed", error=f"unexpected probe error: {type(e).__name__}: {e}")
