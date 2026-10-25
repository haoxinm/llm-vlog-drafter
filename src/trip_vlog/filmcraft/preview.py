"""Preview verification: ffprobe the rendered MP4 and compare it with the sequence (DESIGN §14 steps 2-3)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from trip_vlog.filmcraft.errors import ToolMissing
from trip_vlog.media.tools import find_ffprobe


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class RenderReport:
    ok: bool
    path: str
    checks: list[Check] = field(default_factory=list)
    duration_seconds: float | None = None
    expected_duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: str | None = None
    video_codec: str | None = None
    nb_frames: int | None = None
    expected_frames: int | None = None
    audio_streams: int = 0
    render_seconds: float | None = None
    offline_media: list[str] = field(default_factory=list)
    engine_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "checks"}
        d["checks"] = [c.to_dict() for c in self.checks]
        return d

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def run_ffprobe(path: Path, ffprobe: str | Path | None = None, timeout: float = 60.0) -> dict[str, Any]:
    exe = find_ffprobe(str(ffprobe) if ffprobe else None)
    if exe is None:
        raise ToolMissing("ffprobe was not found; install FFmpeg (it is also needed for media inventory) or set "
                          "media.ffprobe_path / $FFPROBE")
    cp = subprocess.run([str(exe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                        capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                        shell=False, **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}))
    if cp.returncode != 0:
        raise ValueError(f"ffprobe could not read {path}: {cp.stderr.strip()[:300]}")
    return json.loads(cp.stdout)


def _fraction(s: Any) -> Fraction | None:
    try:
        return Fraction(str(s))
    except (ValueError, ZeroDivisionError):
        return None


def evaluate_preview(
    probe: Mapping[str, Any], *, path: str, width: int, height: int, fps: Fraction, frames: int, expect_audio: bool,
    scale: float = 1.0,
) -> RenderReport:
    """Pure comparison of an ffprobe JSON document with the expected sequence properties."""
    rep = RenderReport(ok=False, path=path, expected_frames=frames, expected_duration_seconds=float(Fraction(frames) / fps))
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    rep.audio_streams = len(audio)
    add = lambda n, ok, d="": rep.checks.append(Check(n, bool(ok), d))
    add("has video stream", video is not None)
    if video is not None:
        rep.width, rep.height, rep.video_codec = video.get("width"), video.get("height"), video.get("codec_name")
        rep.fps = video.get("r_frame_rate")
        nb = video.get("nb_frames")
        rep.nb_frames = int(nb) if str(nb).isdigit() else None
        ew, eh = round(width * scale), round(height * scale)
        slack = 0 if scale == 1.0 else 2
        add("dimensions", abs((rep.width or 0) - ew) <= slack and abs((rep.height or 0) - eh) <= slack,
            f"expected {ew}x{eh}, got {rep.width}x{rep.height}")
        got = _fraction(video.get("r_frame_rate"))
        add("frame rate", got == fps, f"expected {fps}, got {rep.fps}")
        if rep.nb_frames is not None:
            add("frame count", abs(rep.nb_frames - frames) <= 1, f"expected {frames}, got {rep.nb_frames}")
    dur = probe.get("format", {}).get("duration")
    try:
        rep.duration_seconds = float(dur)
    except (TypeError, ValueError):
        rep.duration_seconds = None
    tol = float(Fraction(1) / fps) + 0.05
    add("duration", rep.duration_seconds is not None and abs(rep.duration_seconds - rep.expected_duration_seconds) <= tol,
        f"expected {rep.expected_duration_seconds:.3f}s +/- {tol:.3f}, got {rep.duration_seconds}")
    if expect_audio:
        add("audio stream present", len(audio) >= 1, f"{len(audio)} audio stream(s)")
    rep.ok = all(c.ok for c in rep.checks)
    return rep
