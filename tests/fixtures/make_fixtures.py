#!/usr/bin/env python3
"""Generate tiny deterministic media fixtures with ffmpeg (Phase 0 spike). Cross-platform.

Usage: python tests/fixtures/make_fixtures.py [output_dir]   (default: tests/fixtures/generated)
Needs ffmpeg on PATH (or set $FFMPEG).  Outputs: landscape_5s.mp4 (1920x1080 30fps + 440 Hz tone),
portrait_4s.mp4 (1080x1920 + tone), still_1080p.png, music_6s.wav, music_6s.m4a.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "generated"
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        print("error: ffmpeg not found on PATH (set $FFMPEG)", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", "400k", "-maxrate", "400k",
              "-bufsize", "800k", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-shortest"]
    jobs = [
        ["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5", *vcodec, str(out / "landscape_5s.mp4")],
        ["-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=4", *vcodec, str(out / "portrait_4s.mp4")],
        ["-f", "lavfi", "-i", "smptebars=size=1920x1080:rate=1", "-frames:v", "1", str(out / "still_1080p.png")],
        ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6", "-ac", "2", str(out / "music_6s.wav")],
        ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6", "-c:a", "aac", "-b:a", "64k", "-ac", "2",
         str(out / "music_6s.m4a")],
    ]
    for j in jobs:
        subprocess.run(base + j, check=True)
    for p in sorted(out.iterdir()):
        print(f"{p.name}\t{p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
