"""Tiny EditPlan builder for QC / handoff unit tests (no catalog, ffmpeg or FilmCraft)."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from trip_vlog.planning.models import EditPlan, with_hash

A1, A2, A3, AM = ("asset_" + c * 16 for c in "abcd")
HASH = "0" * 64


def clip(cid: str, start: int, dur: int, *, asset: str = A1, src_in: float = 0.0, media: str = "video", role: str = "primary_video",
         policy: str = "natural_sound", dissolve: int = 0, fps: int = 30, moment: str | None = None) -> dict[str, Any]:
    """An edit-plan clip. Frames are sequence frames at ``fps``; source seconds follow from them."""
    still = media == "image"
    sin = round(src_in * fps)
    return {
        "clip_instance_id": cid, "asset_id": asset, "moment_id": moment or f"mom_{cid}", "media_type": media, "anchor": "start",
        "track_role": role, "audio_policy": policy, "requested_duration_seconds": dur / fps,
        "source_in_seconds": None if still else src_in, "source_out_seconds": None if still else src_in + dur / fps,
        "source_in_frame": None if still else sin, "source_out_frame": None if still else sin + dur,
        "timeline_start_frame": start, "duration_frames": dur,
        "transition_in": {"type": "dissolve" if dissolve else "cut", "duration_seconds": dissolve / fps, "duration_frames": dissolve, "alignment": "center"},
        "resolver_notes": [], "rationale": "test"}


def make_plan(*scenes: tuple[str, list[dict[str, Any]]], fps: str = "30", width: int = 1920, height: int = 1080, titles: list[dict[str, Any]] | None = None,
              markers: list[dict[str, Any]] | None = None) -> EditPlan:
    f = Fraction(fps)
    out, pos = [], 0
    for sid, clips in scenes:
        start = min(c["timeline_start_frame"] for c in clips)
        end = max(c["timeline_start_frame"] + c["duration_frames"] for c in clips)
        out.append({"scene_id": sid, "title": sid, "purpose": "test", "target_duration_seconds": max(1.0, float((end - start) / f)),
                    "start_frame": start, "duration_frames": end - start, "clips": clips})
        pos = max(pos, end)
    doc = {"schema_version": "1.1", "plan_hash": HASH, "selection_plan_hash": HASH,
           "project": {"title": "T", "target_duration_seconds": float(pos / f), "aspect_ratio": "16:9", "frame_rate": fps},
           "sequence": {"width": width, "height": height, "fps": fps, "aspect_ratio": "16:9", "still_duration_seconds": 3.0},
           "scenes": out, "markers": markers or [], "titles": titles or [],
           "audio": {"music_policy": "user_supplied_only", "preserve_natural_audio": True, "duck_music_under_speech": True},
           "total_duration_frames": pos, "total_duration_seconds": float(pos / f), "uncertainties": []}
    return with_hash(EditPlan.model_validate(doc))
