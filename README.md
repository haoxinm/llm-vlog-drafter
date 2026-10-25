# llm-vlog-drafter

Draft a travel vlog from a folder of photos and videos, with an LLM. The full design is in [DESIGN.md](DESIGN.md).

**Status:** Phase 0 (FilmCraft spike, see [docs/phase0-filmcraft-spike.md](docs/phase0-filmcraft-spike.md)) and
**Phase 1 (media inventory)** are done. Segmentation, planning, the FilmCraft adapter and the Resolve handoff are Phase 2+.

Originals are never modified. Everything the tool writes goes into a separate *output workspace* that must live outside the media folder.

## Install

Requires Python 3.11+ and FFmpeg (`ffmpeg` and `ffprobe` on `PATH`).

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/trip-vlog --version
.venv/bin/pytest                        # integration tests skip themselves if ffmpeg is missing
```

It is pure Python, so `pipx install .` or `uv tool install .` also work.

### FFmpeg per OS

| OS | Install |
|---|---|
| Windows 10/11 | `winget install Gyan.FFmpeg` or `choco install ffmpeg`, then open a new terminal |
| macOS 13+ | `brew install ffmpeg` (Homebrew prefixes `/opt/homebrew` and `/usr/local` are searched even if not on `PATH`) |
| Linux | `sudo apt install ffmpeg` (or your distribution's package) |

Lookup order for each tool: `media.ffprobe_path` / `media.ffmpeg_path` (or `--ffprobe` / `--ffmpeg`), then `$FFPROBE` / `$FFMPEG`, then `PATH`, then standard install folders.

## Usage

```sh
# 1. Inventory: scan -> probe -> catalog -> thumbnails (re-runs are incremental)
trip-vlog index --media /path/to/trip-media --output /path/to/vlog-project [--config project.yaml]

# 2. See what happened (stage status, failed files, stale assets, probable duplicates/moves)
trip-vlog status --output /path/to/vlog-project [--json]

# 3. Continue after an interruption or after fixing a problem (media folder comes from manifest.json)
trip-vlog resume --output /path/to/vlog-project

# Stub: runs `index`, then says the remaining stages arrive in Phase 2
trip-vlog draft --media ... --output ... --duration 180 --aspect 16:9
```

Configuration: copy [examples/project.yaml](examples/project.yaml), edit, pass with `--config`. Command-line overrides win:
`--fingerprint sha256`, `--no-recursive`, `--follow-symlinks`, `--ffprobe PATH`, or any field via `--set media.thumbnail_max_side=320`.
Without `--config`, `<output>/project.yaml` (the effective config of the last run) is used if present. Invalid values and
unsafe paths (workspace inside the media folder, outputs escaping the workspace, missing media folder) are rejected with exit code 2.

Exit codes: `0` finished (files that could not be probed are reported, not fatal), `1` a stage failed, `2` bad configuration/arguments.

### What the index produces

```
vlog-project/
  project.yaml  manifest.json  README.md
  media_index/
    catalog.sqlite          # single source of truth (assets, analysis cache, empty segments/moments/transcripts tables)
    assets.jsonl            # export regenerated from SQLite, never read back
    inventory_report.json   # skipped/unsupported files, case collisions, duplicates/moves, failed probes
    thumbnails/             # midpoint JPEG per video, EXIF-oriented JPEG per photo
    contact_sheets/         # per-video sheet and per-folder sheets, labeled with asset ID + timestamp
  logs/run.jsonl  logs/errors.jsonl
```

- **Asset IDs** are `asset_` + first 16 hex of SHA-256 of the relative path (POSIX separators, Unicode NFC, case preserved). They survive content changes.
- **`content_version`** is `fast:<sha256 of size, mtime, first 4 MiB, last 4 MiB>` (default) or `sha256:<full hash>`. A changed value marks the asset's
  analysis `stale` (id unchanged). The same fingerprint at another path is reported as a probable move/duplicate and never merged.
- **Probe data**: duration, pixel and display size (after rotation), exact rational frame rate (`30000/1001`) with a VFR flag, rotation, codec, pix_fmt, HDR hint,
  audio stream count, capture time (original UTC offset kept; an unknown local time is never converted to UTC), GPS.
- **Caching**: results are keyed by asset + content_version + tool/settings. A second run reuses everything; touching one file reprocesses only that file.
- **Logs** are JSONL with run ID, stage, asset ID, status (`pending/running/succeeded/partial/failed/skipped`), cache hit/miss, duration and sanitized errors
  (secret-like environment values and `key=value` tokens are masked; the media folder is shown as `<media_root>`).

### Privacy

File names, thumbnails and GPS are personal data. GPS is stored in `catalog.sqlite` but omitted from `assets.jsonl` unless `media.export_gps: true`.
Nothing in Phase 1 contacts the network.

## Platform notes

Windows 10/11 and macOS 13+ are first-class targets, Linux is for development and CI. The code uses `pathlib` only, argument-list subprocesses (no shell),
UTF-8 with `\n` newlines, atomic `os.replace` writes (with short retries for Windows file locks), extended-length paths for long Windows paths, and
generated file names that avoid Windows-invalid characters and reserved names. CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu, macOS and Windows
with Python 3.11 and 3.13. Only Linux has been exercised locally so far; treat Windows/macOS results as unverified until CI has run.

## Known limitations (Phase 1)

- HEIC/HEIF photos need ffprobe support for those formats (Pillow has no HEIC decoder here); without it they are listed as failed, and no thumbnail is made.
- Fast fingerprints include mtime, so a plain copy that does not preserve mtime is not detected as a duplicate (use `--fingerprint sha256`).
  Switching fingerprint mode refreshes `content_version` without marking assets stale, so a real change made at the same moment goes unnoticed.
- A truncated JPEG/PNG that Pillow can still open passes the probe and fails later at the thumbnail step (reported as a thumbnail error).
- HDR clips get ordinary (not tone-mapped) thumbnails; they look flat.
- Failed probes and failed thumbnails are retried on every run; they are not cached.
- Hidden/system skipping follows the fixed name list in `media/scan.py` plus dotfiles and Windows hidden/system attributes; other junk is only skipped by extension.
- Probing is sequential and single-threaded.
- No segmentation, LLM, FilmCraft, preview or Resolve handoff yet (Phase 2+). `trip-vlog draft` only runs `index`.
- Not yet verified on real phone footage (rotation, QuickTime location, VFR) beyond canned ffprobe JSON samples in `tests/fixtures/ffprobe/`.
