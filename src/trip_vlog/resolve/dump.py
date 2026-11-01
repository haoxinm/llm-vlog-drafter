"""Read a Resolve timeline back through the scripting API; classify transitions; detect image-sequence names."""
from __future__ import annotations

import os
import re
from typing import Any

from trip_vlog.resolve.api import method, safe_call, to_number

TOL_FRAMES = 1
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".exr", ".dpx"}
# Resolve lists transitions as ordinary timeline items without a MediaPoolItem. "Final Cut Pro 7" is the
# name Resolve gives imported FCP7 audio transitions (constant-power fades).
TRANSITION_NAME_RE = re.compile(r"dissolve|cross\s*fade|fade|transition|final cut pro 7|\b(?:dip|wipe)\b", re.I)
TRANSITION_MAX_FRAMES = 30  # an unnamed item without media is a transition only if it is this short...
MEDIA_NAME_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")  # ...and its name does not look like a media file
SEQ_RE = re.compile(r"^(.*)\[(\d+)-(\d+)\](.*)$")  # Resolve's image-sequence naming: still_[1080-1229]p.png


def dump_timeline(timeline: Any) -> dict:
    """Read frame rate, resolution, duration, tracks, items and markers through documented calls."""
    errs: list[str] = []
    start = to_number(safe_call(method(timeline, "GetStartFrame"), errors=errs, label="GetStartFrame")) or 0
    end = to_number(safe_call(method(timeline, "GetEndFrame"), errors=errs, label="GetEndFrame"))
    out: dict[str, Any] = {
        "name": safe_call(method(timeline, "GetName"), errors=errs, label="GetName"),
        "start_frame": start, "end_frame": end,
        "duration_frames": (end - start) if end is not None else None,
        "fps": to_number(safe_call(method(timeline, "GetSetting"), "timelineFrameRate", errors=errs, label="GetSetting(timelineFrameRate)")),
        "width": to_number(safe_call(method(timeline, "GetSetting"), "timelineResolutionWidth", errors=errs, label="GetSetting(width)")),
        "height": to_number(safe_call(method(timeline, "GetSetting"), "timelineResolutionHeight", errors=errs, label="GetSetting(height)")),
        "tracks": [], "markers": [], "errors": errs,
    }
    for kind in ("video", "audio"):
        n = int(to_number(safe_call(method(timeline, "GetTrackCount"), kind, default=0, errors=errs, label=f"GetTrackCount({kind})")) or 0)
        for idx in range(1, n + 1):
            tname = safe_call(method(timeline, "GetTrackName"), kind, idx, errors=errs, label="GetTrackName") if True else None
            items = safe_call(method(timeline, "GetItemListInTrack"), kind, idx, default=[], errors=errs, label=f"GetItemListInTrack({kind},{idx})") or []
            rec = []
            for i, it in enumerate(items):
                r = dump_item(it, start, errs)
                r["index"] = i  # position in GetItemListInTrack, used to fetch the live item again
                rec.append(r)
            rec.sort(key=lambda r: (r["start"] if r["start"] is not None else 0))
            classify_items(rec)
            out["tracks"].append({"kind": kind, "index": idx, "name": tname or f"{kind} {idx}", "items": rec})
    mk = safe_call(method(timeline, "GetMarkers"), default={}, errors=errs, label="GetMarkers") or {}
    for fid, m in mk.items():
        f = to_number(fid)
        if f is None:
            continue
        if start > 1000 and f >= start:  # defensive: some builds report absolute frames
            f -= start
        out["markers"].append({"frame": int(f), "name": (m or {}).get("name") or (m or {}).get("note") or "",
                               "color": (m or {}).get("color")})
    out["markers"].sort(key=lambda m: m["frame"])
    return out


def is_transition_name(name: Any) -> bool:
    return bool(name) and TRANSITION_NAME_RE.search(str(name)) is not None


def classify_items(items: list[dict]) -> None:
    """In place: set item['kind'] = 'transition' | 'clip' for one track's dumped items.
    Transition = no MediaPoolItem AND (known transition name OR short item that spans/abuts a cut point or
    clip end of this track). Items that look like media files by name are never transitions. Transitions
    also get centre, nearest_cut and centre_offset (centre minus nearest cut)."""
    cuts: list[float] = []
    for it in items:
        if it.get("has_media_pool_item"):
            cuts += [c for c in (it.get("start"), it.get("end")) if c is not None]
    for it in items:
        it["kind"] = "clip"
        if it.get("has_media_pool_item"):
            continue
        s, e, name = it.get("start"), it.get("end"), it.get("name")
        if s is None or e is None:
            continue
        if name and MEDIA_NAME_RE.search(str(name)):
            continue
        by_name = is_transition_name(name)
        by_shape = (e - s) <= TRANSITION_MAX_FRAMES and any(s - TOL_FRAMES <= c <= e + TOL_FRAMES for c in cuts)
        if not (by_name or by_shape):
            continue
        it["kind"] = "transition"
        centre = (s + e) / 2
        it["centre"] = centre
        near = min(cuts, key=lambda c: abs(c - centre)) if cuts else None
        it["nearest_cut"] = near
        it["centre_offset"] = (centre - near) if near is not None else None


def dump_item(it: Any, tl_start: float, errs: list) -> dict:
    s = to_number(safe_call(method(it, "GetStart"), errors=errs, label="Item.GetStart"))
    e = to_number(safe_call(method(it, "GetEnd"), errors=errs, label="Item.GetEnd"))
    d = to_number(safe_call(method(it, "GetDuration"), errors=errs, label="Item.GetDuration"))
    lo = to_number(safe_call(method(it, "GetLeftOffset"), errors=errs, label="Item.GetLeftOffset"))
    mpi = safe_call(method(it, "GetMediaPoolItem"), errors=errs, label="Item.GetMediaPoolItem")
    path = None
    if mpi is not None:
        path = safe_call(method(mpi, "GetClipProperty"), "File Path", errors=errs, label="GetClipProperty(File Path)")
        if isinstance(path, dict):
            path = path.get("File Path")
    exists = None
    if path:
        try:
            exists = os.path.exists(path)
        except OSError:
            exists = None
    rs = (s - tl_start) if s is not None else None
    re_ = (e - tl_start) if e is not None else None
    if re_ is None and rs is not None and d is not None:
        re_ = rs + d
    return {
        "name": safe_call(method(it, "GetName"), errors=errs, label="Item.GetName"),
        "start": rs, "end": re_, "duration": d, "source_in": lo,
        "has_media_pool_item": mpi is not None, "media_path": path or None,
        "media_exists_on_disk": exists,
        "online": bool(mpi is not None and path and exists is not False),
    }

