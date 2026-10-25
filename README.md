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

# Phase 3b: label video segments and photos with the vision model (runs index + segment first; needs an LLM endpoint, see "LLM provider")
trip-vlog analyze --output /path/to/vlog-project [--media /path/to/trip-media] [--limit N] [--sampling-fps 1.0] [--dry-run]
trip-vlog llm-check [--output /path/to/vlog-project | --config project.yaml] [--endpoint NAME] [--no-image]

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
  media_index/    catalog.sqlite (authoritative), assets.jsonl, moments.jsonl, inventory_report.json, thumbnails/, contact_sheets/, analysis/ (sampled frames + per-segment sheets)
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

Phase 3a adds a provider-neutral LLM gateway (`src/trip_vlog/llm/`). It speaks plain HTTP (Python standard library only; no `anthropic` or `openai`
package and no extra install) to any server that implements an Anthropic- or OpenAI-style API, with a fully configurable endpoint. `analyze` and `llm-check` use it; the baseline `draft` flow never contacts the network.

Configure endpoint *profiles* under `llm:` in `project.yaml` and pick one for text and one for vision:

```yaml
llm:
  mode: live                 # live | record | replay   (env TRIP_VLOG_LLM_MODE overrides)
  text: main                 # profile used for text/planning calls
  vision: main               # profile used for frame/vision calls
  endpoints:
    main:
      api: anthropic_messages        # anthropic_messages | openai_chat | openai_responses
      base_url: https://api.anthropic.com
      path: null                     # null = adapter default (/v1/messages, /v1/chat/completions, /v1/responses)
      model: null                    # else the env var named by model_env; no built-in default
      model_env: TRIP_VLOG_LLM_MODEL
      api_key_env: ANTHROPIC_API_KEY # NAME of the env var holding the key; null = no auth (local servers)
      headers: {}                    # extra headers; values may use ${VAR}; never logged
      timeout_seconds: 120
      max_retries: 2                 # transport retries on 429/5xx/connection errors (backoff + Retry-After)
      structured_output: native      # native | json_prompt | none
      max_tokens: 4096               # cap on a request's max_tokens
      max_tokens_field: null         # e.g. max_completion_tokens for newer OpenAI models
      supports_images: true
      params: {}                     # merged into the request body (temperature, top_p, seed, reasoning, store, ...)
      allow_insecure_http: false     # plain http to a non-loopback host is refused unless true
```

Examples (set only the profile fields that differ from the defaults; keys always come from the environment):

```yaml
# Anthropic  (export ANTHROPIC_API_KEY=...   PowerShell: $env:ANTHROPIC_API_KEY="...")
llm: {endpoints: {main: {api: anthropic_messages, model: <your model id>}}}

# OpenAI chat completions  (OPENAI_API_KEY)
llm: {endpoints: {main: {api: openai_chat, model: <id>, max_tokens_field: max_completion_tokens}}}

# OpenAI Responses API; store:false asks the service not to retain the exchange
llm: {endpoints: {main: {api: openai_responses, model: <id>, params: {store: false}}}}

# OpenAI-compatible cloud host (base_url + path as the host documents; its own key variable)
llm:
  endpoints:
    main: {api: openai_chat, base_url: "https://api.example-host.com", path: /openai/v1/chat/completions,
           api_key_env: EXAMPLE_HOST_KEY, model: <id>, structured_output: json_prompt}

# Local server (Ollama, llama.cpp server, LM Studio, vLLM): no key, loopback only. Ollama and LM Studio serve /v1/...
llm:
  endpoints:
    main: {api: openai_chat, base_url: "http://127.0.0.1:11434", api_key_env: null, model: <local model name>,
           structured_output: json_prompt, supports_images: false}      # set true for a vision model
media: {local_only: true}
```

- **Credentials** are read only from the environment variable named by `api_key_env`; a literal `api_key` in the YAML is refused. Keys are never logged,
  stored in caches, or sent anywhere but the configured endpoint (redirects are not followed). Loopback servers are contacted directly, bypassing proxy variables.
- **Model id** is never hard-coded: `model` in the profile wins, else the environment variable named by `model_env` (default `TRIP_VLOG_LLM_MODEL`); with
  neither, live and record modes fail with a clear message.
- **Wire formats** (`api`): `anthropic_messages` (`x-api-key`, `anthropic-version`, `output_config.format`), `openai_chat` (`Authorization: Bearer`,
  `response_format`), `openai_responses` (`instructions`/`input`, `text.format`). They follow the providers' public API docs but could not be tested against live
  servers here; verify against yours and adapt with `path`, `params`, `max_tokens_field` and `structured_output`. `params` may not set `model`, `messages`,
  `input`, `system` or `instructions`; fields the gateway controls (model, messages, token limit, structured output) win over `params`.
- **Structured output**: `native` sends the provider's JSON-schema option (a sanitized copy: `additionalProperties: false`, no `minimum`/`maxLength`/...; OpenAI
  strict mode also wants every property listed in `required`). `json_prompt` sends no schema parameter and instead appends the schema to the prompt (most local
  servers need this); `none` sends nothing. In every mode the gateway extracts the first JSON value from the reply (fences and prose tolerated), validates it
  against the full schema, and on failure re-asks with the validation errors only (`llm.schema_retries`, default 2; no extra media is re-sent).
  Refusals, content-filter stops and `max_tokens` truncation raise typed errors (`LLMRefusalError`, `LLMTruncatedError`); output is never silently cut.
- **Local-only**: an endpoint whose `base_url` host is loopback (`127.x`, `localhost`, `::1`) is "local". `media.local_only: true` requires the profiles used by
  `llm.text` and `llm.vision` to be local (checked when the config loads). Each call's log record includes `endpoint` and `local`.
- **Response cache**: `<workspace>/cache/llm/<fingerprint>.json` (`llm.cache_dir`). The fingerprint is a SHA-256 over the prompt template id + version, system text,
  schema, text parts, each image's *source file* hash plus the preparation settings (`image_max_side`, `image_quality`, prep version; not the re-encoded bytes,
  which can differ across OSes), model id, `max_tokens`, effort, and the profile's `api` and `params`. It excludes `base_url`, `path`, headers, timeouts and keys,
  so a recording made against one host replays against another. A changed frame, prompt version, schema, model or params is a miss; everything else is a hit with no call.
- **Record / replay** (tests and CI use replay only, no key needed): `TRIP_VLOG_LLM_MODE=replay` (or `llm.mode: replay`) returns stored responses and raises
  `ReplayMissError` naming the fingerprint and template on a miss. `record` calls the real endpoint and also saves each validated response to the fixture dir
  (`llm.fixture_dir` or `TRIP_VLOG_LLM_FIXTURE_DIR`; default the cache dir; tests use `tests/fixtures/llm/`). Replay must use the same model string as the recording.
- **Images** are downscaled (longest side `llm.image_max_side`, default 1568), re-encoded as JPEG (`llm.image_quality`, default 85) and stripped of EXIF/GPS before
  upload; originals are never modified.
- **Prompts** live in `src/trip_vlog/prompts/<id>.v<N>.md` with `id`/`version` front matter that must match the file name. Text derived from media (file names, EXIF,
  transcripts) is wrapped by `llm.untrusted_section(...)` as inert data blocks.
- **Cost and privacy**: with a remote endpoint, sampled frames, prompts and any transcripts you include are sent to that provider (and some services retain
  requests by default; see `params: {store: false}` above). Each call logs prompt id/version, model, token usage (input, output, cache read/creation), latency,
  attempts, request id, endpoint and local/remote to `logs/run.jsonl` (stage `llm`), never keys or image bytes. The response cache holds model output and the text
  sent (not image bytes); treat the workspace as private. Use a loopback endpoint with `local_only` or `replay` to avoid any upload.

## Vision labeling (`analyze`)

`trip-vlog analyze` turns the deterministic segments into candidate moments with a vision model (DESIGN §7.2, §7.4). It runs `index` and `segment` first,
then the resumable `analyze` stage. `draft --planner baseline` never needs it; `draft --planner llm` is still rejected until Phase 4 (which will run `analyze` for you).

```sh
trip-vlog analyze --output ./vlog --media ./trip --dry-run          # counts only: segments, frames, images, requests, endpoint, local/remote
trip-vlog analyze --output ./vlog --media ./trip --limit 3          # first 3 assets (by path) as a cheap trial
trip-vlog analyze --output ./vlog --sampling-fps 2                  # sample 2 frames per second (re-samples and re-labels videos)
```
```powershell
$env:ANTHROPIC_API_KEY = "..."      # the variable named by api_key_env of your endpoint
trip-vlog analyze --output C:\vlogs\lisbon --media D:\Trips\Lisbon --dry-run
trip-vlog analyze --output C:\vlogs\lisbon --media D:\Trips\Lisbon --limit 3 --sampling-fps 1
```

- **Sampling**: `media.vision_sampling_fps` (default 1.0, 0 < fps <= 30; CLI `--sampling-fps FLOAT` on `analyze` and `draft` overrides it and is recorded in
  `manifest.json` under `cli_overrides`). Each segment is cut into equal slices of about `1/fps` s and the frame at the centre of each slice is decoded with ffmpeg
  (one process per segment, accurate seek, orientation corrected, longest side `llm.image_max_side`). More than `media.vision_max_frames_per_segment` (default 8) frames
  are thinned evenly, keeping the first and last. Frames live in `media_index/analysis/frames/<asset>/` under names derived from the content version and the sampling
  settings; per-segment contact sheets (labelled with segment ID, frame index and timestamp) in `media_index/analysis/sheets/`.
  `media.fine_sampling_enabled` / `fine_sampling_fps` (denser resampling around the best part) are **not implemented yet** and only produce a warning.
- **What the model receives** (`media.vision_input`): `frames` (default; one image per sampled frame, each announced by a text line with segment ID, frame index and
  source time, better grounding) or `contact_sheet` (one grid image per segment, cheaper). Segments are batched up to `media.vision_batch_max_images` (default 16)
  images and the endpoint's `max_tokens`; a segment's frames are never split across requests. Photos are sent one image each, batched the same way.
  The model never produces timestamps: it may name a best run of frame *indices* (`best_frames`), which the code converts to frame-rounded source seconds clamped to the
  segment. A different sub-range becomes the moment's `source_in/out_seconds` (the segment bounds stay in `segment_start/end_seconds`; `moment_id` and `segment_id`
  are the baseline ones). Label schemas: `schemas/segment_labels.schema.json`, `schemas/photo_labels.schema.json`; prompts `segment_labels.v1`, `photo_labels.v1` on top of `media_analyst.v1`.
- **Validation and fallback**: unknown segment IDs are ignored, out-of-range or reversed `best_frames` reject that item (recorded in `logs/errors.jsonl`), the rest of the
  reply is kept. Missing or rejected items are re-asked once (fresh request, bypassing the response cache); what still fails keeps its deterministic baseline moment with
  `labeled: false` and `label_error`, and is retried by the next `analyze`. A fatal endpoint error (auth, connection, config) stops the stage; finished batches are kept.
- **Photos**: near-duplicates are found with a 64-bit dHash (Pillow only; `media.photo_duplicate_distance`, default 6 bits) and share a `duplicate_group` on their moments.
- **Caching**: a label is cached per segment by asset content version, segment bounds, prompt id/version/text hash, schema hash, model, endpoint `api`/`params`, sampling
  settings (fps, frame cap, image size) and `vision_input`; the gateway's response cache sits underneath. Re-runs send nothing; changing only the target duration or
  story settings does not re-run analysis. Moments are in `catalog.sqlite` and exported to `media_index/moments.jsonl` (no GPS; GPS is never sent either).
- **Cost and privacy**: with a remote endpoint, up to `vision_max_frames_per_segment` frames per segment (or one sheet) and each photo are sent, downscaled and stripped of
  EXIF/GPS, plus the file's *base name* (never folders) wrapped as untrusted data (`media.vision_send_filenames: false` omits it). Use `--dry-run` to see the numbers first
  and a loopback endpoint with `media.local_only: true` to keep everything on the machine. Tokens used are logged per request and summed in `status`.

### Checking an endpoint (`llm-check`)

```sh
trip-vlog llm-check --output ./vlog                  # every endpoint used by llm.text / llm.vision: a text request, then an image request
trip-vlog llm-check --config project.yaml --endpoint main --no-image
```
```powershell
trip-vlog llm-check --config .\project.yaml --endpoint main
```

One tiny live request per check (a 2-field JSON reply; then a generated red square, which must be recognised), bypassing every cache and never recorded to fixtures. The table
shows endpoint, api, URL (no query string or secrets), local/remote, model, structured-output mode, ok/FAIL, latency, tokens and the error class with a short message. Exit
0 if all checks pass, 1 if any fails, 2 for a bad configuration or unknown `--endpoint`.

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
