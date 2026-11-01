"""Shared helpers for the FilmCraft adapter tests (fake server + sample spec)."""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.timeline_spec import MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_filmcraft.py"
FAKE_ARGV = [sys.executable, str(FAKE)]
FAKE_MEDIA = ["landscape_5s.mp4", "still_1080p.png", "portrait_4s.mp4", "music_6s.wav"]


def make_fake_media(media: Path) -> Path:
    media.mkdir(parents=True, exist_ok=True)
    for n in FAKE_MEDIA:
        (media / n).write_bytes(b"x" * 64)
    return media


def sample_spec(media: Path, fps: Fraction | int | str = 30, *, end_marker: int | None = None) -> TimelineSpec:
    """landscape video + still with dissolve on V1, portrait (no audio) + title on V2, wav on A2 with gain, a marker.

    Durations are defined in seconds (3.5 s, 3 s, 2 s, 6 s) and converted to whole frames, so the same spec
    shape works at 25, 30 and 30000/1001 fps (195 frames at 30 fps).
    """
    fps = Fraction(fps)
    sec = lambda s: int(round(float(fps) * s))
    f = lambda frames: float(Fraction(frames) / fps)
    P = lambda n: str(media / n)
    n1, n2, n3, n4 = sec(3.5), sec(3), sec(2), sec(6)
    return TimelineSpec(
        "Main Draft", 1920, 1080, fps, ("V1", "V2", "A1", "A2"),
        placements=(
            Placement("c1", P("landscape_5s.mp4"), "video", "V1", 0, n1, 0.5, 0.5 + f(n1), True),
            Placement("c2", P("still_1080p.png"), "image", "V1", n1, n2, 0.0, None, False, None, TransitionIn("dissolve", 15)),
            Placement("c3", P("portrait_4s.mp4"), "video", "V2", n3, n3, 1.0, 1.0 + f(n3), False),
            Placement("c4", P("music_6s.wav"), "audio", "A2", 0, n4, 0.0, f(n4), False, -6.0),
        ),
        markers=(MarkerSpec(n3 if end_marker is None else end_marker, "Scene A", "first scene"),),
        titles=(TitleSpec("Hello Title", 0, n3, "V2"),),
    )


def make_adapter(tmp_path: Path, **kw) -> tuple[FilmCraftAdapter, Path, Path]:
    media = make_fake_media(tmp_path / "media")
    ws = tmp_path / "workspace"
    kw.setdefault("argv_prefix", FAKE_ARGV)
    kw.setdefault("request_timeout", 30.0)
    return FilmCraftAdapter(ws, media, **kw), media, ws


def log_requests(ws: Path, command: str | None = None, tool: str | None = None) -> list[dict]:
    recs = [json.loads(l) for l in (ws / "filmcraft" / "command_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [r for r in recs if r.get("dir") == "request" and r.get("method") == "tools/call"]
    if command:
        recs = [r for r in recs if r.get("command") == command]
    if tool:
        recs = [r for r in recs if r.get("tool") == tool]
    return recs


import pytest  # noqa: E402


@pytest.fixture
def fc(tmp_path):
    """(adapter, media_dir, workspace) wired to the fake FilmCraft server; closed on teardown."""
    adapter, media, ws = make_adapter(tmp_path)
    try:
        yield adapter, media, ws
    finally:
        adapter.close()
