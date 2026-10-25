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

If `trip-vlog` is "not recognized" (common on Windows when pip installs outside a venv, into a `Scripts` folder
that is not on `PATH`), run it through Python instead: `python -m trip_vlog <command> ...` takes the same
arguments. Or activate the venv first (PowerShell: `.venv\Scripts\Activate.ps1`), which puts `trip-vlog` on `PATH`.

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

# Optional, Resolve Studio only: finish the handoff inside Resolve (see "Finish in Resolve" below)
trip-vlog resolve-finish --output /path/to/vlog-project [--dry-run] [--project-name NAME] [--keep-on-failure]
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
  resolve_handoff/  timeline.xml, timeline.otio, media_manifest.csv, edit_plan.json, project_reference.json, handoff_report.md,
                    resolve_finish_report.json/.md (only after `resolve-finish`)
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

### Finish in Resolve (`resolve-finish`, optional, Studio only)

`trip-vlog resolve-finish --output WORKSPACE` runs on your machine with **DaVinci Resolve Studio open** and *External scripting using = Local*
(Preferences > System > General, then restart Resolve). It uses only Resolve's documented scripting API through an allowlisted, logged wrapper
(no LLM, no MCP layer, no `eval`/`exec`), after `draft` has completed its handoff:

1. creates a **new** project `<title> (trip-vlog vNNN)` (never reuses, modifies or deletes an existing project) and sets frame rate and resolution from the edit plan;
2. imports `resolve_handoff/timeline.xml` with a unique timeline name;
3. repairs media that arrived offline or was misread as an image sequence (`IMG_1042.JPG` becomes `IMG_[1042-1191].JPG` in Resolve) with
   `MediaPoolItem.ReplaceClip(<path from media_manifest.csv>)`; paths outside the manifest are refused;
   Resolve refuses `ReplaceClip` for a still that has numbered siblings in its folder (`stars_-318.jpg` next to `stars_-319.jpg`; it reads them
   as an image sequence). For stills only, the fallback copies that one file (`shutil.copy2`, the original is never modified) to
   `resolve_handoff/stills/<key>/<name>` and relinks the clip to the copy. Keep `resolve_handoff/stills/` together with the Resolve project;
4. adds scene markers that are missing (an existing marker with the same frame and name is not duplicated);
5. **experimental:** creates the titles of the timeline spec as editable Text+ titles on their track and range; a title that cannot be verified
   by read-back is rolled back (only items this run created) and listed for manual recreation. The baseline planner emits no titles yet;
6. reads the timeline back, compares it with the FilmCraft timeline spec and writes `resolve_handoff/resolve_finish_report.json` and `.md`
   (every API call, repairs, titles, pass/fail table). The new project is always kept and saved.

`--dry-run` prints the planned steps and changes nothing. Exit codes: `0` finished (the report lists titles that need manual work), `1` required
checks failed, `2` preconditions or arguments (no completed handoff, stale handoff, project name already exists), `3` Resolve scripting unavailable
(diagnosis printed, including this Python version against what Resolve's `fusionscript` library supports). `trip-vlog status` shows the run.
It is not part of `draft`; the plain FCP7 XML import remains the supported path without Studio. See
[docs/resolve-verification.md](docs/resolve-verification.md#resolve-finish) for how to run it on Windows and macOS and what to send back.

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

## LLM provider

Phase 3a adds the provider-neutral LLM gateway in `src/trip_vlog/llm/`. No CLI command uses it yet (vision labeling and transcription come next);
the baseline `draft` flow still never contacts the network.

```sh
pip install -e ".[llm]"                  # installs the optional official `anthropic` SDK (imported lazily)
export ANTHROPIC_API_KEY=...             # macOS / Linux
```

```powershell
pip install -e ".[llm]"
$env:ANTHROPIC_API_KEY="..."             # Windows PowerShell (current session only)
```

- **Credentials** are read by the SDK from the environment only. They are never accepted from `project.yaml`, never logged and never stored.
- **Model id** is never hard-coded. It comes from `media.llm_model` if set, otherwise from the `TRIP_VLOG_LLM_MODEL` environment variable (config wins, so a
  project pins its model; the env var is a per-machine default). With neither, the Anthropic provider fails with a clear message.
- **Providers**: `media.vision_provider` (frames) and `media.llm_provider` (text) accept `anthropic` or `replay`. `media.local_only: true` rejects cloud providers.
- **Settings** under `media`: `llm_max_retries` (SDK transport retries, 2), `llm_schema_retries` (re-asks with validation errors, 2), `llm_timeout_seconds`,
  `llm_image_max_side` (1568), `llm_image_quality` (85), `llm_cache_dir` (`cache/llm`, relative to the workspace), `llm_fixture_dir` (record/replay store; default = cache dir).
- **Structured output**: every request carries a JSON schema. The provider is sent a sanitized copy (no `minimum`/`maxLength`/..., `additionalProperties: false`),
  and the gateway validates the full original client-side; on failure it retries by sending back the validation errors (no extra media).
  Refusals and `max_tokens` truncation raise typed errors (`LLMRefusalError`, `LLMTruncatedError`); output is never silently cut.
- **Response cache**: results are stored per request fingerprint in `<workspace>/cache/llm/<fingerprint>.json`. The fingerprint is a SHA-256 over the prompt
  template id + version, system text, schema, text parts, the hash of each image as actually sent, model id, `max_tokens` and effort (no paths), so a changed
  frame, prompt version, schema or model is a miss and everything else is a hit with no API call.
- **Record / replay** (tests and CI use replay only, no key needed): `TRIP_VLOG_LLM_MODE=replay` returns stored responses and raises `ReplayMissError`
  (naming the fingerprint and template) on a miss. `TRIP_VLOG_LLM_MODE=record` calls the real provider and also saves each validated response to the fixture dir
  (`media.llm_fixture_dir` or `TRIP_VLOG_LLM_FIXTURE_DIR`; tests use `tests/fixtures/llm/`). The default mode is `off`. The `replay` provider name always replays.
  Replay must use the same model string as the recording, since the model is part of the fingerprint.
- **Prompts** live in `src/trip_vlog/prompts/<id>.v<N>.md` with `id`/`version` front matter that must match the file name; the version is logged and fingerprinted.
  Text derived from media (file names, EXIF, transcripts) is wrapped by `llm.untrusted_section(...)` as inert data blocks.
- **Cost and privacy**: with a cloud provider, the sampled frames, prompts and any transcripts you include are sent to that provider. Frames are downscaled,
  re-encoded as JPEG and stripped of EXIF/GPS before upload; original files are never modified. Each call logs prompt id/version, model, token usage (input, output,
  cache read/creation), latency, attempts and request id to `logs/run.jsonl` (stage `llm`), never keys or image bytes. Use `local_only` or `replay` to avoid any upload.
  The response cache holds model output and the text sent (not image bytes); treat the workspace as private.

## Platform notes

Windows 10/11 and macOS 13+ are first-class targets, Linux is for development and CI. The code uses `pathlib` only, argument-list subprocesses (no shell),
UTF-8 with `\n` newlines, atomic `os.replace` writes (with short retries for Windows file locks), extended-length paths for long Windows paths, and
generated file names that avoid Windows-invalid characters and reserved names. CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu, macOS and Windows
with Python 3.11 and 3.13. Only Linux has been exercised locally so far; treat Windows/macOS results as unverified until CI has run.

## Known limitations

- **Phase 3 is only partly implemented (the LLM gateway exists, see "LLM provider"):** no vision labeling, transcription, LLM outline/selection planning, critic or natural-language revisions. The baseline planner
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
