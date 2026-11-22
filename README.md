# llm-vlog-drafter

Draft a travel vlog from a folder of photos and videos, with an LLM. The full design is in [DESIGN.md](DESIGN.md).

**Status:** Phase 0 (FilmCraft spike, see [docs/phase0-filmcraft-spike.md](docs/phase0-filmcraft-spike.md)), Phase 1 (media inventory) and
**Phase 2 (deterministic end-to-end slice)** are done: `trip-vlog draft` turns a media folder into a reopened FilmCraft project, a rendered preview,
a QC report and a Resolve handoff package, using a baseline planner with no LLM. **Phase 3+ (LLM vision labeling, LLM story/selection planning,
critic, natural-language revisions) is not implemented yet**; `--planner llm` is rejected.

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

## FilmCraft (editing engine)

`draft` drives [FilmCraft](https://github.com/storytold/filmcraft) through its documented MCP interface (`filmcraft-cli mcp`). It was developed and
verified against FilmCraft 0.13.0 on Linux. Install it for your OS:

| OS | Install |
|---|---|
| Windows 10/11 | Installer (MSI) or the portable zip from the FilmCraft releases page; the CLI is `filmcraft-cli.exe` |
| macOS 13+ | Download the separate `filmcraft-cli` zip from the releases page (the app bundle alone does not contain it); `chmod +x filmcraft-cli` |
| Linux | A release package, or build it: `cargo build --release -p filmcraft-cli` (binary in `target/release/filmcraft-cli`) |

The binary is found via `--filmcraft PATH` / `filmcraft.executable`, then `$FILMCRAFT_CLI`, then `PATH`, then standard install folders. An explicitly
configured path that does not exist is an error. The discovered version and capabilities are written to `filmcraft/capability_snapshot.json`, and the
version is recorded in `manifest.json`. A build that lacks a required capability stops assembly with a message naming it.
`pip install -e ".[otio]"` adds OpenTimelineIO, which the handoff step then uses to reload `timeline.otio` as an extra check.

## Usage

```sh
# One shot: inventory -> segment -> baseline plan -> (approval) -> FilmCraft project -> preview -> QC -> Resolve handoff
trip-vlog draft --media /path/to/trip-media --output /path/to/vlog-project --duration 180 --aspect 16:9 \
                [--config project.yaml] [--planner baseline] [--filmcraft /path/to/filmcraft-cli] [--auto-approve]

# Default (workflow.approval = stop_after_outline): the run stops after the plan, BEFORE anything is created in FilmCraft (exit code 3).
#   read  <output>/plans/plan_summary.md   (scenes, clips, source ranges, notes)
trip-vlog approve --output /path/to/vlog-project      # records approval for this exact plan (plan hash); refuses an invalid plan
trip-vlog resume  --output /path/to/vlog-project      # builds the project, preview, QC and handoff

trip-vlog status  --output /path/to/vlog-project [--json]   # every stage incl. awaiting_approval, approval, FilmCraft version, next step

# Inventory only (Phase 1)
trip-vlog index --media /path/to/trip-media --output /path/to/vlog-project
```

- `--auto-approve` (or `workflow.approval: auto_approve`) skips the stop and records `approval.mode = "automatic"` in `manifest.json`.
- Approval is bound to the SHA-256 of the plan content. Changing `--duration`, the media, or the plan file invalidates it and the run stops again.
- `plans/edit_plan.json` is what gets executed. It is re-validated (schema, IDs, source ranges, durations, transitions) at assemble time, so a plan that
  was edited into an invalid state is rejected before FilmCraft is started. If you hand-edit it, `plan_hash` must be updated too or validation reports a mismatch.
- Re-running on unchanged inputs does nothing: every stage is cached (inputs hash plus a check that its artifact still exists) and `assemble` is a no-op.
  Changing `--duration` re-plans and builds a **new versioned project** (`filmcraft/Trip Vlog Draft_v002.fcproj`); probing and segmentation come from the cache.
  A failed or interrupted assembly leaves its numbered `.fcproj` behind, never reused; `resume` builds the next version, so clips are never appended twice.
- `resume` continues the pipeline recorded in `manifest.json`; the media folder is taken from there. Settings persist in `<output>/project.yaml`.

Exit codes: `0` finished (QC warnings and unprobeable files are reported, not fatal), `1` a stage failed, `2` bad configuration/arguments (including
`--planner llm`), `3` stopped at the approval gate, `4` `approve` refused (no plan yet, or the plan is invalid), `130` interrupted.

Configuration: copy [examples/project.yaml](examples/project.yaml), edit, pass with `--config`. Command-line overrides win:
`--duration`, `--aspect`, `--fingerprint sha256`, `--no-recursive`, `--ffprobe PATH`, or any field via `--set media.thumbnail_max_side=320`.
Invalid values and unsafe paths (workspace inside the media folder, outputs escaping the workspace) are rejected with exit code 2.

### Workspace layout

```
vlog-project/
  project.yaml  manifest.json  README.md          # manifest: versions, approval, FilmCraft version, per-stage status
  media_index/    catalog.sqlite (authoritative), assets.jsonl, moments.jsonl, inventory_report.json, thumbnails/, contact_sheets/
  plans/          edit_plan.json, plan_summary.md, selection_plan.json, *.schema.json, validation_report.json
  filmcraft/      Trip Vlog Draft_v001.fcproj ..., capability_snapshot.json, command_log.jsonl, execution_journal.json, mapping_report.json
  preview/        draft.mp4, qc_report.json, contact_sheet_timeline.jpg, render_report.json
  resolve_handoff/  timeline.xml, timeline.otio, media_manifest.csv, edit_plan.json, project_reference.json, handoff_report.md
  logs/           run.jsonl, errors.jsonl
```

### Timeline and QC

Track layout: video and stills on V1, overlays V2, natural audio of V1 clips on A1 (linked), music A2, narration A3. `audio_policy` `muted` places video
only; `ambient_low` applies a constant -12 dB (`filmcraft.ambient_low_gain_db`); no ducking or automation is generated. The adapter places dissolves
centre-aligned only: a dissolve the resolver aligned to the cut start/end is placed as a hard cut and recorded in the handoff report ("Mapping warnings").

`preview/qc_report.json` has a pass/warn/fail status per check: media online, project reopens in a fresh FilmCraft process and matches the plan, no gaps on V1,
duplicate scenes, still durations, total duration within tolerance (`project.duration_tolerance_pct` / `_seconds`), preview playable (ffprobe), black frames
(ffmpeg `blackdetect`), and portrait-in-landscape (flagged for manual review). `contact_sheet_timeline.jpg` shows frames just before and after every cut,
taken from the rendered preview.

### Resolve handoff

`resolve_handoff/` holds the interchange files exported by FilmCraft (post-processed to absolute media paths and re-validated), `media_manifest.csv`,
the validated `edit_plan.json`, `project_reference.json` (path + hash of the `.fcproj`; the project and the media are **not** copied) and `handoff_report.md`.
FCP7 XML is the primary file (it imported cleanly in Resolve 21.1 with the Phase 0 fixture); OTIO is secondary (imports, but media linking with absolute
paths is not yet verified). FCPXML, EDL and AAF are never exported unless you list them in `filmcraft.interchange_formats`; on Resolve 21.1 they crashed,
froze and failed to open respectively, and the report then carries a prominent warning. If the export fails, the manifest, plan, preview and report are still
written and the report says what failed.

**The report states that Resolve import is unverified** unless a passing `resolve_verification.json` is present. To verify, run `tools/resolve_verify.py`
on a machine with Resolve open (see [docs/resolve-verification.md](docs/resolve-verification.md)), then `trip-vlog resume` to refresh the report.
Media references are absolute: moving the package or the media to another machine breaks the links until they are relinked.

### Phase 1 details (inventory)

- **Asset IDs** are `asset_` + first 16 hex of SHA-256 of the relative path (POSIX separators, Unicode NFC, case preserved). They survive content changes.
- **`content_version`** is `fast:<sha256 of size, mtime, first 4 MiB, last 4 MiB>` (default) or `sha256:<full hash>`. A changed value marks the asset's
  analysis `stale` (id unchanged). The same fingerprint at another path is reported as a probable move/duplicate and never merged.
- **Probe data**: duration, pixel and display size (after rotation), exact rational frame rate (`30000/1001`) with a VFR flag, rotation, codec, pix_fmt, HDR hint,
  audio stream count, capture time (original UTC offset kept; an unknown local time is never converted to UTC), GPS.
- **Caching**: results are keyed by asset + content_version + tool/settings. Touching one file reprocesses only that file.
- **Logs** are JSONL with run ID, stage, asset ID, status (`pending/running/succeeded/partial/failed/skipped/awaiting_approval`), cache hit/miss, duration and
  sanitized errors (secret-like values are masked; the media folder is shown as `<media_root>`).

### Privacy

File names, thumbnails and GPS are personal data. GPS is stored in `catalog.sqlite` but omitted from `assets.jsonl` unless `media.export_gps: true`.
Nothing in Phase 1 or 2 contacts the network; FilmCraft runs locally and is confined to the workspace for writes and to the media folder + workspace for reads.

## Platform notes

Windows 10/11 and macOS 13+ are first-class targets, Linux is for development and CI. The code uses `pathlib` only, argument-list subprocesses (no shell),
UTF-8 with `\n` newlines, atomic `os.replace` writes (with short retries for Windows file locks), extended-length paths for long Windows paths, and
generated file names that avoid Windows-invalid characters and reserved names. CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu, macOS and Windows
with Python 3.11 and 3.13. Only Linux has been exercised locally so far; treat Windows/macOS results as unverified until CI has run.

## Known limitations

- **Phase 3+ is not implemented:** no vision labeling, transcription, LLM outline/selection planning, critic or natural-language revisions. The baseline planner
  is chronological with fixed clip lengths, stills at a default duration, no music or narration, hard cuts, and no titles.
- Resolve import is unverified by this tool (see above). Titles are not carried by any interchange format; audio fades appear as dissolves in OTIO;
  FCPXML drops non-V1 transitions; EDL carries one video track. Only the FilmCraft 0.13.0 / Linux combination has been run; Windows and macOS are unverified until CI has run.
- Hand-editing `edit_plan.json` requires updating `plan_hash` yourself; there is no helper yet.
- Interrupted assemblies leave their numbered `.fcproj` in `filmcraft/`; it is not cleaned up automatically.
- Black-frame detection runs on the preview only and is reported as a warning; subjective checks (speech truncation, audio conflicts) need the Phase 5 critic.
- HEIC/HEIF photos need ffprobe support for those formats (Pillow has no HEIC decoder here); without it they are listed as failed, and no thumbnail is made.
- Fast fingerprints include mtime, so a plain copy that does not preserve mtime is not detected as a duplicate (use `--fingerprint sha256`).
  Switching fingerprint mode refreshes `content_version` without marking assets stale, so a real change made at the same moment goes unnoticed.
- A truncated JPEG/PNG that Pillow can still open passes the probe and fails later at the thumbnail step (reported as a thumbnail error).
- HDR clips get ordinary (not tone-mapped) thumbnails; they look flat.
- Failed probes and failed thumbnails are retried on every run; they are not cached.
- Probing is sequential and single-threaded.
- Not yet verified on real phone footage (rotation, QuickTime location, VFR) beyond canned ffprobe JSON samples in `tests/fixtures/ffprobe/`.
