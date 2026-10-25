"""Stdlib-only OTIO reader and expected-timeline JSON loader (no opentimelineio needed)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from trip_vlog.resolve.paths import normalize_path


def _rt(o: Any) -> tuple[float, float] | None:
    if isinstance(o, dict) and "value" in o and "rate" in o:
        return float(o["value"]), float(o["rate"])
    return None


def _frames(rt: tuple[float, float] | None, fps: float) -> int:
    if rt is None or rt[1] == 0:
        return 0
    return int(round(rt[0] * fps / rt[1]))


def _range(o: Any, fps: float) -> tuple[int, int] | None:
    if not isinstance(o, dict):
        return None
    st, du = _rt(o.get("start_time")), _rt(o.get("duration"))
    if st is None or du is None:
        return None
    return _frames(st, fps), _frames(du, fps)


def _media_ref(clip: dict) -> dict | None:
    refs = clip.get("media_references")
    if isinstance(refs, dict) and refs:
        key = clip.get("active_media_reference_key") or "DEFAULT_MEDIA"
        return refs.get(key) or next(iter(refs.values()))
    mr = clip.get("media_reference")
    return mr if isinstance(mr, dict) else None


def schema_of(o: Any) -> str:
    return str(o.get("OTIO_SCHEMA", "")).split(".")[0] if isinstance(o, dict) else ""


def read_otio_expected(path: str | os.PathLike) -> dict:
    """Parse an .otio (JSON) file into the expected-timeline dict. No OTIO library needed."""
    p = Path(path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    # Resolve relative target_urls against the .otio file's ABSOLUTE directory (cwd-independent).
    return otio_to_expected(doc, base_dir=p.resolve().parent)


def otio_to_expected(doc: dict, base_dir: str | os.PathLike | None = None) -> dict:
    if schema_of(doc) != "Timeline":
        raise ValueError("not an OTIO Timeline document")
    stack = doc.get("tracks") or {}
    tracks = [t for t in stack.get("children", []) if schema_of(t) == "Track"]
    meta = (doc.get("metadata") or {}).get("filmcraft", {}).get("settings", {})
    # frame rate: filmcraft settings, else global_start_time, else first clip's rate
    fps = None
    fr = meta.get("frame_rate")
    if isinstance(fr, dict) and fr.get("num") and fr.get("den"):
        fps = fr["num"] / fr["den"]
    if fps is None:
        g = _rt(doc.get("global_start_time"))
        fps = g[1] if g else None
    if fps is None:
        for t in tracks:
            for c in t.get("children", []):
                r = _range(c.get("source_range"), 1) if schema_of(c) == "Clip" else None
                rt = _rt((c.get("source_range") or {}).get("duration"))
                if rt:
                    fps = rt[1]
                    break
            if fps:
                break
    fps = float(fps or 24.0)

    out_tracks, transitions, total = [], 0, 0
    for t in tracks:
        kind = str(t.get("kind", "Video")).lower()
        pos, clips = 0, []
        for c in t.get("children", []):
            sch = schema_of(c)
            if sch == "Transition":
                transitions += 1
                continue
            sr = _range(c.get("source_range"), fps)
            if sch == "Gap":
                pos += sr[1] if sr else 0
                continue
            if sch != "Clip" or sr is None:
                continue
            mr = _media_ref(c)
            src = None
            if mr and schema_of(mr) == "ExternalReference" and mr.get("target_url"):
                src = normalize_path(mr["target_url"], base_dir, windows=False)
            clips.append({
                "name": c.get("name") or "", "source_path": src,
                "record_start_frame": pos, "record_end_frame": pos + sr[1],
                "source_in_frame": sr[0], "source_out_frame": sr[0] + sr[1],
            })
            pos += sr[1]
        total = max(total, pos)
        out_tracks.append({"name": t.get("name") or f"{kind} {len(out_tracks) + 1}", "kind": kind, "clips": clips})

    markers = []

    def collect(o: Any) -> None:
        for m in (o.get("markers") or []) if isinstance(o, dict) else []:
            mr = _range(m.get("marked_range"), fps)
            if mr:
                markers.append({"name": m.get("name") or "", "frame": mr[0]})

    collect(stack)
    for t in tracks:
        collect(t)
    gs = _frames(_rt(doc.get("global_start_time")), fps)
    if gs:
        for m in markers:
            m["frame"] -= gs
    return {
        "name": doc.get("name") or "", "fps": fps,
        "width": meta.get("width"), "height": meta.get("height"),
        "duration_frames": total, "transitions": transitions,
        "markers": markers, "tracks": out_tracks, "derived_from": "otio",
    }


def load_expected_json(path: str | os.PathLike) -> dict:
    exp = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("fps", "tracks"):
        if k not in exp:
            raise ValueError(f"expected timeline JSON lacks '{k}'")
    exp.setdefault("markers", [])
    exp.setdefault("transitions", None)
    base = Path(path).resolve().parent
    for t in exp["tracks"]:
        for c in t.get("clips", []):
            if c.get("source_path"):
                c["source_path"] = normalize_path(c["source_path"], base, windows=False)
    return exp

