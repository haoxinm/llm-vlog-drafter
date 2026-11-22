"""Label schemas, client-side validation and the conversion from model labels to candidate moments (DESIGN §6.3, §7.2).

The model never produces timestamps. For videos it may name a best run of *sampled frame indices*; this module converts
those indices to frame-rounded source seconds, clamped to the segment.
"""

from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction
from typing import Any, Sequence

from trip_vlog.media.segment import DEFAULT_FPS, moment_tags
from trip_vlog.planning.duration import frames_to_float, parse_fps

LABEL_VERSION = 1  # bump when the moment shape built from a label changes

EDITORIAL_TAGS = ("establishing", "people", "food", "nature", "city", "transport", "activity", "detail",
                  "reaction", "transition", "scenic", "night", "other")
QUALITY_LEVELS = ("low", "ok", "good")
MAX_ENTITIES = 12
MAX_DESCRIPTION_CHARS = 600

_LEVEL = {"type": ["string", "null"], "enum": [*QUALITY_LEVELS, None]}


def _item_schema(video: bool) -> dict[str, Any]:
    props: dict[str, Any] = {
        "segment_id": {"type": "string", "description": "Copied exactly from the segment header."},
        "description": {"type": "string", "description": "Grounded: only what is visible."},
        "visible_entities": {"type": "array", "items": {"type": "string"}},
        "event": {"type": ["string", "null"]},
        "editorial_tags": {"type": "array", "items": {"type": "string", "enum": list(EDITORIAL_TAGS)}, "minItems": 1},
        "quality": {
            "type": "object",
            "properties": {"sharpness": dict(_LEVEL), "stability": dict(_LEVEL), "exposure": dict(_LEVEL)},
            "required": ["sharpness", "stability", "exposure"],
            "additionalProperties": False,
        },
        "interest": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "uncertain": {"type": "boolean"},
        "uncertainty_note": {"type": ["string", "null"]},
    }
    if video:
        props["best_frames"] = {
            "type": ["object", "null"],
            "description": "Best consecutive run of the frame indices shown for this segment; null = the whole segment.",
            "properties": {"first_index": {"type": "integer", "minimum": 0}, "last_index": {"type": "integer", "minimum": 0}},
            "required": ["first_index", "last_index"],
            "additionalProperties": False,
        }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _schema(video: bool) -> dict[str, Any]:
    name = "segment_labels" if video else "photo_labels"
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://trip-vlog.local/schemas/{name}:{LABEL_VERSION}",
        "title": name,
        "description": ("Vision labels for video segments; best_frames are indices into the frames shown. No timestamps."
                        if video else "Vision labels for still photos."),
        "type": "object",
        "properties": {"segments": {"type": "array", "items": _item_schema(video)}},
        "required": ["segments"],
        "additionalProperties": False,
    }


def segment_labels_schema() -> dict[str, Any]:
    return _schema(True)


def photo_labels_schema() -> dict[str, Any]:
    return _schema(False)


SCHEMAS = {"segment_labels": segment_labels_schema, "photo_labels": photo_labels_schema}


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write_schemas(directory: Any) -> list[Any]:
    """Write both label schemas into `directory` as `<name>.schema.json` (what `schemas/` in the repository holds)."""
    from pathlib import Path

    from trip_vlog.state import atomic_write_json

    out = []
    for name, build in SCHEMAS.items():
        p = Path(directory) / f"{name}.schema.json"
        atomic_write_json(p, build())
        out.append(p)
    return out


# ---------------------------------------------------------------- validation
def _dedupe(values: Sequence[str], limit: int) -> list[str]:
    seen: list[str] = []
    for v in values:
        v = " ".join(str(v).split())
        if v and v.lower() not in (x.lower() for x in seen):
            seen.append(v)
    return seen[:limit]


def validate_label(item: dict[str, Any], *, n_frames: int | None) -> tuple[dict[str, Any] | None, str | None]:
    """Referential and range checks the JSON schema cannot express. Returns (normalised item, None) or (None, error).

    `n_frames` is the number of frames sent for a video segment (indices must be 0..n_frames-1) or None for a photo, which
    has no best_frames.
    """
    sid = item.get("segment_id")
    desc = " ".join(str(item.get("description") or "").split())
    if not desc:
        return None, "empty description"
    for k in ("interest", "confidence"):
        v = item.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
            return None, f"{k} {v!r} is not a number in [0, 1]"
    bf = item.get("best_frames")
    if bf is not None:
        if n_frames is None:
            return None, "best_frames given for a photo"
        a, b = bf.get("first_index"), bf.get("last_index")
        if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int) or not isinstance(b, int):
            return None, "best_frames indices must be integers"
        if not (0 <= a < n_frames and 0 <= b < n_frames):
            return None, f"best_frames {a}..{b} is outside the {n_frames} frame(s) shown (valid 0..{n_frames - 1})"
        if b < a:
            return None, f"best_frames last_index {b} is before first_index {a}"
    out = {
        "segment_id": sid,
        "description": desc[:MAX_DESCRIPTION_CHARS],
        "visible_entities": _dedupe(item.get("visible_entities") or [], MAX_ENTITIES),
        "event": (" ".join(str(item["event"]).split()) or None) if item.get("event") else None,
        "editorial_tags": [t for t in dict.fromkeys(item.get("editorial_tags") or []) if t in EDITORIAL_TAGS] or ["other"],
        "quality": {k: (item.get("quality") or {}).get(k) if (item.get("quality") or {}).get(k) in QUALITY_LEVELS else None
                    for k in ("sharpness", "stability", "exposure")},
        "interest": round(float(item["interest"]), 4),
        "confidence": round(float(item["confidence"]), 4),
        "uncertain": bool(item.get("uncertain")),
        "uncertainty_note": (" ".join(str(item["uncertainty_note"]).split()) or None) if item.get("uncertainty_note") else None,
        "best_frames": {"first_index": bf["first_index"], "last_index": bf["last_index"]} if bf is not None else None,
    }
    return out, None


# ---------------------------------------------------------------- indices -> seconds
def segment_fps(seg: dict[str, Any]) -> Fraction:
    try:
        return parse_fps(seg.get("fps")) if seg.get("fps") else DEFAULT_FPS
    except ValueError:
        return DEFAULT_FPS


def best_range(seg: dict[str, Any], frame_numbers: Sequence[int], first: int, last: int) -> tuple[int, int] | None:
    """Source frame range [in, out) for the sampled-frame indices first..last, or None for the whole segment.

    Each sampled frame stands for the stretch of the segment closest to it: its territory starts halfway to the previous
    sample (the segment start for the first) and ends halfway to the next (the segment end for the last). The result is
    clamped to the segment and is at least one frame long.
    """
    s, e = int(seg["start_frame"]), int(seg["end_frame"])
    n = len(frame_numbers)
    if n == 0 or not (0 <= first <= last < n):
        return None
    lo = s if first == 0 else (frame_numbers[first - 1] + frame_numbers[first] + 1) // 2
    hi = e if last == n - 1 else (frame_numbers[last] + frame_numbers[last + 1] + 1) // 2
    lo = min(max(lo, s), e - 1)
    hi = min(max(hi, lo + 1), e)
    if (lo, hi) == (s, e):
        return None
    return lo, hi


# ---------------------------------------------------------------- moments
def labeled_moment(baseline: dict[str, Any], seg: dict[str, Any], label: dict[str, Any], *, frames: Sequence[dict[str, Any]],
                   content_version: str | None, model: str, prompt: dict[str, Any], contact_sheet: str | None = None,
                   duplicate_group: str | None = None) -> dict[str, Any]:
    """§6.3 candidate moment from a validated label. The moment_id and segment_id are the baseline's (shared IDs)."""
    m = dict(baseline)
    sub = None
    bf = label.get("best_frames")
    if bf is not None and seg.get("start_frame") is not None and frames:
        sub = best_range(seg, [f["frame"] for f in frames], bf["first_index"], bf["last_index"])
    fps = segment_fps(seg)
    if sub is not None:
        m["source_in_seconds"] = frames_to_float(sub[0], fps)
        m["source_out_seconds"] = frames_to_float(sub[1], fps)
    m.update({
        "description": label["description"],
        "visible_entities": label["visible_entities"],
        "event": label["event"],
        "quality": label["quality"],
        "editorial_tags": label["editorial_tags"],
        "technical_tags": moment_tags(seg),
        "interest": label["interest"],
        "confidence": label["confidence"],
        "uncertain": label["uncertain"],
        "uncertainty_note": label["uncertainty_note"],
        "evidence": [{"type": "frame", "timestamp_seconds": f["timestamp_seconds"], "artifact": f["artifact"], "frame_index": f["index"]}
                     for f in frames],
        "segment_start_seconds": seg.get("start_seconds"),
        "segment_end_seconds": seg.get("end_seconds"),
        "best_frames": ({"first_index": bf["first_index"], "last_index": bf["last_index"], "source_in_seconds": m["source_in_seconds"],
                         "source_out_seconds": m["source_out_seconds"]} if sub is not None else None),
        "contact_sheet": contact_sheet,
        "content_version": content_version,
        "source": "llm",
        "labeled": True,
        "model": model,
        "prompt": prompt,
    })
    if baseline.get("media_type") == "image":
        m["duplicate_group"] = duplicate_group
    return m


def unlabeled_moment(baseline: dict[str, Any], error: str | None, duplicate_group: str | None = None) -> dict[str, Any]:
    """Deterministic fallback: the baseline moment, flagged so the planner knows it has no description."""
    m = dict(baseline)
    m.update(labeled=False, label_error=error)
    if baseline.get("media_type") == "image":
        m["duplicate_group"] = duplicate_group
    return m


if __name__ == "__main__":  # python -m trip_vlog.analysis.labels [dir]
    import sys

    for p in write_schemas(sys.argv[1] if len(sys.argv) > 1 else "schemas"):
        print(p)
