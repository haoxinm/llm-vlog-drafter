#!/usr/bin/env bash
# Generate tiny deterministic media fixtures with ffmpeg (Phase 0 spike).
# Usage: tests/fixtures/make_fixtures.sh [output_dir]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/generated}"
mkdir -p "$out"
F=(ffmpeg -hide_banner -loglevel error -y)

# 5 s, 1920x1080, 30 fps landscape video with 440 Hz tone (stereo AAC 48k)
"${F[@]}" -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=5" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=5" \
  -c:v libx264 -preset ultrafast -b:v 400k -maxrate 400k -bufsize 800k -pix_fmt yuv420p -g 30 \
  -c:a aac -b:a 64k -ac 2 -shortest "$out/landscape_5s.mp4"

# 4 s, 1080x1920 portrait video with tone
"${F[@]}" -f lavfi -i "testsrc2=size=1080x1920:rate=30:duration=4" \
  -f lavfi -i "sine=frequency=660:sample_rate=48000:duration=4" \
  -c:v libx264 -preset ultrafast -b:v 400k -maxrate 400k -bufsize 800k -pix_fmt yuv420p -g 30 \
  -c:a aac -b:a 64k -ac 2 -shortest "$out/portrait_4s.mp4"

# 1920x1080 PNG still
"${F[@]}" -f lavfi -i "smptebars=size=1920x1080:rate=1" -frames:v 1 "$out/still_1080p.png"

# 6 s audio-only (WAV, 48k stereo) and m4a
"${F[@]}" -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=6" -ac 2 "$out/music_6s.wav"
"${F[@]}" -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=6" -c:a aac -b:a 64k -ac 2 "$out/music_6s.m4a"
ls -l "$out"
