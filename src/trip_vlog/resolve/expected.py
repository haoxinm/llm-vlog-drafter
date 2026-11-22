"""The expected Resolve timeline, derived from the FilmCraft timeline spec of the built project (DESIGN 15.6).

The result has the shape ``trip_vlog.resolve.compare.compare_timeline`` expects (the same JSON ``resolve_verify.py``
documents): tracks of clips in frames, record end exclusive. It is built from the *effective* spec (dissolves that
FilmCraft downgraded to cuts are already cuts), not from the interchange file, so Resolve is compared with the
FilmCraft read-back and not with its own input.
"""

from __future__ import annotations

import os
import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.timeline_spec import Placement, TimelineSpec, parse_track

_TRANSITIONITEM = re.compile(r"<\s*transitionitem[\s>]", re.I)


def count_xml_transitions(xml_path: Path) -> int | None:
    """Number of ``<transitionitem>`` elements (video dissolves and audio fades) in an FCP7 XML; None if unreadable."""
    try:
        return len(_TRANSITIONITEM.findall(Path(xml_path).read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return None


def _frames(seconds: float | int | Fraction | None, fps: Fraction) -> int:
    if seconds is None:
        return 0
    return round(Fraction(seconds).limit_denominator(1_000_000) * fps)


def _clip(p: Placement, fps: Fraction, *, audio: bool) -> dict[str, Any]:
    still = p.media_kind == "image"
    s_in = 0 if still else _frames(p.source_in_seconds, fps)
    s_out = p.duration_frames if still else _frames(p.source_out_seconds, fps)
    return {"name": os.path.basename(p.source_path.replace("\\", "/")), "source_path": p.source_path,
            "record_start_frame": p.timeline_start_frame, "record_end_frame": p.end_frame,
            "source_in_frame": s_in, "source_out_frame": s_out, "clip_instance_id": p.clip_instance_id,
            "kind": "audio" if audio else p.media_kind}


def expected_from_spec(spec: TimelineSpec, *, markers: Sequence[Mapping[str, Any]] | None = None,
                       transitions: int | None = None, title_names: Mapping[int, str] | None = None) -> dict[str, Any]:
    """Expected timeline for ``compare_timeline``.

    ``markers``: ``[{name, frame}]`` (default: the spec's markers). ``transitions``: expected transition count
    (default: dissolves in the spec). ``title_names``: spec title index -> the name Resolve shows for the title clip; only
    the listed titles are expected (None: all titles, named by their text). Titles are optional clips without media.
    """
    fps = spec.fps
    rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for p in spec.placements:
        rows.setdefault(p.track, []).append((p.timeline_start_frame, _clip(p, fps, audio=p.media_kind == "audio")))
        at = p.audio_track
        if at is not None and p.media_kind == "video":
            rows.setdefault(at, []).append((p.timeline_start_frame, _clip(p, fps, audio=True)))
    for i, t in enumerate(spec.titles):
        if title_names is not None and i not in title_names:
            continue
        name = t.text if title_names is None else title_names[i]
        rows.setdefault(t.track, []).append((t.start_frame, {
            "name": name, "source_path": None, "record_start_frame": t.start_frame, "record_end_frame": t.end_frame,
            "source_in_frame": 0, "source_out_frame": t.duration_frames, "kind": "title", "title_index": i}))
    tracks = []
    for name in sorted(rows, key=lambda n: (parse_track(n)[0] != "V", parse_track(n)[1])):
        tracks.append({"name": name, "kind": "video" if name.startswith("V") else "audio",
                       "clips": [c for _, c in sorted(rows[name], key=lambda r: r[0])]})
    dissolves = sum(1 for p in spec.placements if p.transition_in.type == "dissolve")
    return {
        "name": spec.sequence_name, "fps": float(fps), "width": spec.width, "height": spec.height,
        "duration_frames": spec.end_frame,
        "transitions": dissolves if transitions is None else transitions,
        "markers": [{"name": m["name"], "frame": int(m["frame"])} for m in markers] if markers is not None
        else [{"name": m.name, "frame": m.frame} for m in spec.markers],
        "tracks": tracks, "derived_from": "filmcraft_timeline_spec",
    }
