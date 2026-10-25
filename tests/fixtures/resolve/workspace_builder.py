"""Minimal draft workspaces for ``resolve-finish`` tests: only the files the command reads, written consistently.

``build_workspace`` makes media files, a timeline spec, the execution journal, manifest.json and a ``resolve_handoff/``
folder (FCP7 XML in the style FilmCraft exports, media_manifest.csv, edit_plan.json, project_reference.json).
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape

from trip_vlog.filmcraft.timeline_spec import MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn
from trip_vlog.handoff.media_manifest import COLUMNS, write_media_manifest
from trip_vlog.state import Manifest, atomic_write_json

PROJECT = "Trip Vlog Draft"


def file_url(p: str) -> str:
    q = Path(p).as_posix()
    return "file://localhost" + (q if q.startswith("/") else "/" + q)


def make_spec(media: Path, still: str = "still_1080p.png", *, titles=(), dissolve=False, markers=True) -> TimelineSpec:
    P = lambda n: str(media / n)
    pl = [
        Placement("clip_001", P("landscape_5s.mp4"), "video", "V1", 0, 90, 0.5, 3.5, True),
        Placement("clip_002", P(still), "image", "V1", 90, 60, 0.0, None, False, None,
                  TransitionIn("dissolve", 14) if dissolve else TransitionIn()),
        Placement("clip_003", P("music_6s.wav"), "audio", "A2", 0, 150, 0.0, 5.0, False, -6.0),
    ]
    mk = (MarkerSpec(0, "Scene 1", "scene_001"), MarkerSpec(90, "Scene 2", "scene_002")) if markers else ()
    return TimelineSpec("Main Draft", 1920, 1080, Fraction(30), ("V1", "V2", "A1", "A2"), tuple(pl), mk, tuple(titles))


def xml_for(spec: TimelineSpec, marker_list=None) -> str:
    """FCP7 XML in the shape FilmCraft writes: every clip names its file once, later uses repeat the file id."""
    seen: dict[str, str] = {}

    def file_el(path: str) -> str:
        if path in seen:
            return f'<file id="{seen[path]}"/>'
        seen[path] = f"file-{len(seen) + 1}"
        return f'<file id="{seen[path]}"><name>{escape(Path(path).name)}</name><pathurl>{escape(file_url(path))}</pathurl></file>'

    def clip(n: int, p: Placement, audio: bool) -> str:
        fps = float(spec.fps)
        s_in = 0 if p.media_kind == "image" else round(p.source_in_seconds * fps)
        return (f'<clipitem id="clipitem-{n}"><name>{escape(Path(p.source_path).name)}</name><start>{p.timeline_start_frame}</start>'
                f'<end>{p.end_frame}</end><in>{s_in}</in><out>{s_in + p.duration_frames}</out>{file_el(p.source_path)}</clipitem>')

    vt: dict[int, list[str]] = {i: [] for i in range(1, spec.video_track_count + 1)}
    at: dict[int, list[str]] = {i: [] for i in range(1, spec.audio_track_count + 1)}
    n = 0
    for p in sorted(spec.placements, key=lambda p: p.timeline_start_frame):
        if p.media_kind in ("video", "image"):
            n += 1
            idx = int(p.track[1:])
            if p.transition_in.type == "dissolve":
                vt[idx].append(f"<transitionitem><start>{p.timeline_start_frame - 7}</start><end>{p.timeline_start_frame + 7}</end></transitionitem>")
            vt[idx].append(clip(n, p, False))
        if p.audio_track:
            n += 1
            at[int(p.audio_track[1:])].append(clip(n, p, True))
    marks = "".join(f"<marker><name>{escape(m['name'])}</name><in>{m['frame']}</in><out>-1</out></marker>"
                    for m in (marker_list if marker_list is not None else [{"name": m.name, "frame": m.frame} for m in spec.markers]))
    v = "".join(f"<track>{''.join(x)}</track>" for x in vt.values())
    a = "".join(f"<track>{''.join(x)}</track>" for x in at.values())
    return (f'<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE xmeml><xmeml version="4"><sequence id="sequence-1">'
            f'<name>{escape(spec.sequence_name)}</name><duration>{spec.end_frame}</duration><media><video>{v}</video><audio>{a}</audio></media>'
            f'{marks}</sequence></xmeml>')


def build_workspace(tmp: Path, *, still: str = "still_1080p.png", titles=(), dissolve=False, markers=True,
                    handoff_status: str = "succeeded", write_xml: bool = True, xml_markers=None) -> dict:
    ws = tmp / "ws"
    media = tmp / "media"
    media.mkdir(parents=True, exist_ok=True)
    for n in ("landscape_5s.mp4", still, "music_6s.wav", "unused.mp4"):
        (media / n).write_bytes(b"x" * 64)
    spec = make_spec(media, still, titles=titles, dissolve=dissolve, markers=markers)
    fc = ws / "filmcraft"
    fc.mkdir(parents=True)
    spec_file = fc / f"{PROJECT}_v001.timeline_spec.json"
    atomic_write_json(spec_file, spec.to_dict())
    run = {"version": 1, "project_name": PROJECT, "status": "complete", "project_path": f"filmcraft/{PROJECT}_v001.fcproj",
           "spec_hash": spec.spec_hash(), "spec_file": f"filmcraft/{spec_file.name}", "downgraded_dissolves": [],
           "filmcraft_version": "0.13.0", "project_sha256": "0" * 64}
    (fc / f"{PROJECT}_v001.fcproj").write_bytes(b"fcproj")
    atomic_write_json(fc / "execution_journal.json", {"schema_version": 1, "runs": [run]})
    hd = ws / "resolve_handoff"
    hd.mkdir()
    if write_xml:
        (hd / "timeline.xml").write_text(xml_for(spec, xml_markers), encoding="utf-8")
    used = set(spec.asset_paths)
    rows = []
    for i, n in enumerate(sorted(p.name for p in media.iterdir())):
        ap = str(media / n)
        row = {c: "" for c in COLUMNS}
        row.update(asset_id=f"asset_{i:016x}", absolute_path=ap, relative_path=n, media_type="video", content_version="fast:x",
                   used_in_timeline="yes" if ap in used else "no", exists_on_disk="yes", probe_status="ok")
        rows.append(row)
    write_media_manifest(hd / "media_manifest.csv", rows)
    plan = {"plan_hash": "h" * 8, "project": {"title": "Iceland Trip"},
            "sequence": {"width": 1920, "height": 1080, "fps": "30"},
            "markers": [{"marker_id": f"marker_{m.comment}", "scene_id": m.comment, "name": m.name, "frame": m.frame} for m in spec.markers]}
    atomic_write_json(hd / "edit_plan.json", plan)
    atomic_write_json(hd / "project_reference.json", {"project_version": 1, "project_path": str(fc / f"{PROJECT}_v001.fcproj")})
    m = Manifest(ws / "manifest.json")
    m.set(pipeline="draft", media_root=str(media),
          filmcraft={"project": run["project_path"], "project_version": 1, "spec_hash": spec.spec_hash(), "version": "0.13.0"})
    m.stages["handoff"] = {"status": handoff_status}
    m.save()
    return {"ws": ws, "media": media, "spec": spec, "run": run, "handoff": hd}


def title(text="Iceland", start=0, dur=60, track="V2", **kw) -> TitleSpec:
    return TitleSpec(text, start, dur, track, **kw)
