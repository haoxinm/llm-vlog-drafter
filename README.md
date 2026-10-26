# llm-vlog-drafter

Draft a travel vlog from a folder of photos and videos, with an LLM. The full design is in [DESIGN.md](DESIGN.md).

**Status:** Phase 0 (FilmCraft spike, see [docs/phase0-filmcraft-spike.md](docs/phase0-filmcraft-spike.md)), Phase 1 (media inventory) and
**Phase 2 (deterministic end-to-end slice)** are done: `trip-vlog draft` turns a media folder into a reopened FilmCraft project, a rendered preview,
a QC report and a Resolve handoff package, using a baseline planner with no LLM. **Phase 3 and 4 (LLM vision labeling, story outline, LLM clip selection
and titles) are implemented** (`draft --planner llm`, see "LLM draft"); the baseline planner stays the default until the LLM path has been tried on real footage.
The critic and natural-language revisions are not implemented yet.

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
                [--config project.yaml] [--planner baseline|llm] [--filmcraft /path/to/filmcraft-cli] [--auto-approve]

# Default (workflow.approval = stop_after_outline): the run stops after the plan, BEFORE anything is created in FilmCraft (exit code 3).
#   read  <output>/plans/plan_summary.md   (scenes, clips, source ranges, notes)
trip-vlog approve --output /path/to/vlog-project      # records approval for this exact plan (plan hash); refuses an invalid plan
trip-vlog resume  --output /path/to/vlog-project      # builds the project, preview, QC and handoff

trip-vlog status  --output /path/to/vlog-project [--json]   # every stage incl. awaiting_approval, approval, FilmCraft version, next step

# Inventory only (Phase 1)
trip-vlog index --media /path/to/trip-media --output /path/to/vlog-project

# Phase 3b: label video segments and photos with the vision model (runs index + segment first; needs an LLM endpoint, see "LLM provider")
trip-vlog analyze --output /path/to/vlog-project [--media /path/to/trip-media] [--limit N] [--sampling-fps 1.0] [--dry-run]
trip-vlog llm-check [--output /path/to/vlog-project | --config project.yaml] [--endpoint NAME] [--no-image] [--speech [--no-llm]]

# Phase 3c: audio features + optional speech transcription (backend none by default; see "Speech transcription")
trip-vlog transcribe --output /path/to/vlog-project [--media /path/to/trip-media] [--limit N] [--dry-run]

# Phase 4a: LLM story outline from the labeled moments, then a stop for your approval (see "Story outline"; runs index ... analyze first)
trip-vlog outline --output /path/to/vlog-project [--media /path/to/trip-media] [--duration 180] [--dry-run] [--auto-approve]

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

Exit codes: `0` finished (QC warnings and unprobeable files are reported, not fatal), `1` a stage failed, `2` bad configuration/arguments, `3` stopped at the approval gate, `4` `approve` refused (nothing to approve yet, or the plan/outline is invalid), `130` interrupted.

Configuration: copy [examples/project.yaml](examples/project.yaml), edit, pass with `--config`. Command-line overrides win:
`--duration`, `--aspect`, `--fingerprint sha256`, `--no-recursive`, `--ffprobe PATH`, or any field via `--set media.thumbnail_max_side=320`.
Invalid values and unsafe paths (workspace inside the media folder, outputs escaping the workspace) are rejected with exit code 2.

### Workspace layout

```
vlog-project/
  project.yaml  manifest.json  README.md          # manifest: versions, approval, FilmCraft version, per-stage status
  media_index/    catalog.sqlite (authoritative), assets.jsonl, moments.jsonl, transcripts.jsonl, inventory_report.json, thumbnails/, contact_sheets/, analysis/ (sampled frames + per-segment sheets)
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
   by read-back is rolled back (only items this run created) and listed for manual recreation. Both planners can emit titles (see "Titles");
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
then the resumable `analyze` stage. `draft --planner baseline` never needs it; `draft --planner llm` runs it for you (as does `outline`).

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

## Story outline (`outline`)

`trip-vlog outline` is the first half of LLM editorial planning (DESIGN §8.1, §9.2, §9.5): from the labeled moments it asks a text model for a **story outline** (premise,
ordered scenes with a role, tone, duration budget and candidate moment IDs, optional opening hook, missing coverage, things to confirm) and then **stops for your approval**.
It selects no clips and touches no FilmCraft; `draft --planner llm` continues from the approved outline (see "LLM draft").
The text model is the endpoint named by `llm.text` (see "LLM provider").

```sh
trip-vlog outline --output ./vlog --media ./trip --duration 180 --dry-run     # moments in the prompt, estimated size, endpoint, local/remote; sends nothing
trip-vlog outline --output ./vlog --media ./trip --duration 180               # index, segment, audio, transcribe, analyze, outline -> stops (exit 3)
#   read  ./vlog/plans/story_outline.md        (what the story editor proposed, per scene with the moment descriptions)
#   edit  ./vlog/plans/story_outline.json      (optional: titles, order, durations, moment IDs)
trip-vlog approve --output ./vlog                                             # says "approved story outline ..."
trip-vlog resume  --output ./vlog                                             # later phases continue from the approved outline
```
```powershell
$env:ANTHROPIC_API_KEY = "..."      # the variable named by api_key_env of the llm.text endpoint
trip-vlog outline --output C:\vlogs\lisbon --media D:\Trips\Lisbon --duration 180 --dry-run
trip-vlog outline --output C:\vlogs\lisbon --media D:\Trips\Lisbon --duration 180
notepad C:\vlogs\lisbon\plans\story_outline.json                           # optional edits
trip-vlog approve --output C:\vlogs\lisbon
trip-vlog resume  --output C:\vlogs\lisbon
```

- **Workflow**: `outline` -> review/edit -> `approve` -> `resume` -> (next phase) selection, resolve, validate, assemble. With `--auto-approve` (or `workflow.approval: auto_approve`)
  the stop is skipped and `manifest.json` records `outline_approval.mode = "automatic"`. Approval is bound to the SHA-256 of the outline *content*: reformatting the file keeps it,
  any real edit withdraws it, and the next `resume` stops again ("outline changed since approval").
- **`approve` approves whatever is waiting and says which**: the story outline when `outline_approval` stopped (or the workspace ran `outline`), else the edit plan of the baseline
  `draft` path (unchanged). If both were ever waiting, the outline comes first. `approve --outline` / `approve --plan` force the choice. It refuses (exit 4) when nothing
  succeeded yet or the file is invalid; the message lists the findings.
- **What the model sees** (`planning.*`): one compact record per candidate moment of a present video/photo asset, in chronological order: a JSON header with only code-made fields
  (`id`, `n`, `kind`, `when` = local capture time without any GPS, `day`, `at`/`dur` seconds, `orient`, `interest`, `conf`, `quality`, `tags`, `audio` hints, `dup` group, `uncertain`, `unlabeled`,
  `speech`) and, when there is any, one `<untrusted_media_data>` block with the description, a verbatim speech excerpt (`planning.transcript_excerpt_chars`, default 160; 0 = none)
  and the file's base name (`media.vision_send_filenames`). Text from the media is always inside such a block and the prompt says it is data, never instructions. No images are sent.
  Settings (title, target duration, story mode, style, audience) and the editorial constraints follow; prompts: `story_editor.v1` (system, DESIGN §9.2) and `story_editor_input.v1`.
- **Too many moments**: above `planning.max_moments_in_prompt` (default 400) the catalog is reduced deterministically: `must_exclude` moments are removed, every `must_include` moment is kept,
  then the best-interest moment of each time cluster (`planning.cluster_gap_seconds`, default 300 s) among duplicate-group representatives, then the other group representatives, then the
  rest, by interest. What was left out and why is in `plans/story_outline_report.json` (`selection`) and in `story_outline.md`.
- **`must_include` / `must_exclude`** are lists of references, not free text: a `moment_id`, `segment_id`, `asset_id`, a path relative to the media folder (`day2/hike.mp4`), a glob
  (`day2/*.mp4`) or a folder with a trailing slash (`day2/`). A `must_include` reference is satisfied when at least one moment it resolves to is in the outline; one that matches
  nothing is reported under `missing_coverage`. `preferred_topics` is free text and is passed to the story editor as a preference.
- **Validation and repair** (deterministic, `planning/outline.py`). Repaired and recorded in the report (and shown under "Checks" in the summary): unknown moment IDs, moments not offered
  to the model, duplicates (a moment is used by one scene; the hook moment may appear once more in its chronological scene), moments matching `must_exclude`, an opening hook longer
  than `editorial.opening_hook_seconds` (shortened), scene budgets outside `project.duration_tolerance_pct`/`_seconds` of the target (all non-hook scenes scaled proportionally to the
  exact target), hook fields left over when no hook is used, scene IDs (`scene_01`, ... assigned by code). Reported as warnings: `story_mode_used` differing from `project.story_mode`,
  an out-of-order scene when `story_mode: chronological`, a `must_include` that matches nothing (also added to `missing_coverage`). Everything that would change the story is an **error** instead: a scene left without moments,
  a hook that is not allowed (`allow_nonlinear_hook: false`) or whose scene/moment is inconsistent, an uncovered `must_include`. Errors are sent back to the model **once**
  (`planning.outline_retries`) with the previous reply; if they remain the stage fails with the findings and writes `plans/story_outline_report.json`. Schema violations are retried by the
  gateway first (`llm.schema_retries`).
- **Your edits are checked, not rewritten**: when you edit `story_outline.json`, `resume` re-validates it without the LLM (any usable moment may be used, not only those that fit in the
  prompt; budgets off target are a warning). Errors fail the `outline_approval` stage and `approve` refuses; fix the file and run `resume`.
- **Caching**: the `outline` stage is keyed by the exact request text (catalog, settings including the target duration, constraints), prompt versions, response schema, model and endpoint
  `api`/`params`, tolerance and retry settings. Unchanged re-runs make no request and never touch an edited outline. Changing `--duration` (or a label, a transcript, the model...) creates a new
  outline; if you had edited the previous one, a copy is kept as `plans/story_outline.<hash>.bak.json`. The gateway's response cache sits underneath.
- **Files** (in `plans/`): `story_outline.json` (the document you approve), `story_outline.md` (read this), `story_outline_report.json` (model, prompt versions, tokens, each attempt's findings
  and repairs, selection), `story_outline.schema.json` and `story_outline_response.schema.json` (also in `schemas/`; the reply schema has no `scene_id`/`schema_version`, every property is required
  and optional values are nullable, so it works with strict structured-output backends).
- **Privacy**: a remote `llm.text` endpoint receives descriptions, local capture times, speech excerpts and base file names (no images, no GPS). `outline --dry-run` shows the endpoint and says
  `remote`/`local`; `media.local_only: true` forces a loopback endpoint. Descriptions and transcripts are personal data: treat the workspace as private.

## LLM draft (`draft --planner llm`)

The full LLM path (DESIGN §8, §9.5, §20 Phase 4): label the footage, propose a story, **you approve the story**, an LLM chooses the clips, code resolves them to exact frames and
validates, then the usual FilmCraft assembly, preview, QC and handoff. The baseline planner remains the default (`--planner baseline`); switch the default only after trying this on your footage.

```sh
export ANTHROPIC_API_KEY=...        # or the variable your llm.text / llm.vision endpoints name; see "LLM provider"
trip-vlog analyze --output ./vlog --media ./trip --dry-run                  # optional: what would be sent, to which endpoint
trip-vlog draft --planner llm --media ./trip --output ./vlog --duration 180 \
                --set editorial.opening_title=true --set project.title="Lisbon 2026"
#   exit 3: index, segment, audio, transcribe, analyze and outline are done; read ./vlog/plans/story_outline.md, edit story_outline.json if you like
trip-vlog approve --output ./vlog          # approves the story outline (exact content hash)
trip-vlog resume  --output ./vlog          # LLM clip selection -> resolve -> validate -> FilmCraft -> preview -> QC -> handoff
#   read ./vlog/plans/plan_summary.md (scenes, clips with moment descriptions, why, titles, repairs) and ./vlog/preview/draft.mp4
trip-vlog status  --output ./vlog
```
```powershell
$env:ANTHROPIC_API_KEY = "..."
trip-vlog draft --planner llm --media D:\Trips\Lisbon --output C:\vlogs\lisbon --duration 180 --set editorial.opening_title=true
notepad C:\vlogs\lisbon\plans\story_outline.json      # optional
trip-vlog approve --output C:\vlogs\lisbon
trip-vlog resume  --output C:\vlogs\lisbon
```
`trip-vlog outline` followed by `draft --planner llm` on the same workspace reuses the approved outline (cached, no new request). `--auto-approve` runs straight through and records both approvals as automatic.

- **Stages**: `scan, probe, catalog, thumbnails, segment, audio, transcribe, analyze, outline, outline_approval, plan, approval, assemble, preview, qc, handoff`. `plan` is the LLM selection.
- **Approval**: the human checkpoint is the **outline** (DESIGN §9.5). A plan made from the approved outline and not touched since is approved automatically and recorded as
  `approval.mode = "derived_from_outline"` (with the outline hash and how the outline was approved), because the selection only follows the story you approved and is validated in code. It stops for
  its own review when you set `workflow.plan_review: true`, and whenever `plans/edit_plan.json` differs from what the plan stage wrote (a hand edit needs `trip-vlog approve` again). Editing the outline withdraws
  its approval and, after you approve again, makes a new plan.
- **What the planner sees** (second request, `timeline_planner.v1` + `timeline_planner_input.v1`): the approved outline (premise, each scene's role, tone, budget and candidate moments in your order) and the
  compact record of **only those candidate moments**, with `use` (the longest clip that fits the moment) and `best` (its best sub-range) added; project settings and rules (`must_include`/`must_exclude`, similar-shot limit,
  transitions, no invented context); and a plain-words capability summary (track roles, cut and dissolve, sound policies, titles; never editor command names, built from the plan enums and the FilmCraft capability
  snapshot when one exists). Text from the media and the outline's wording is wrapped as untrusted data. It returns, per scene, clips (`moment_id`, `target_duration_seconds`, `anchor`, `track_role`, `audio_policy`,
  `transition_in`, `rationale`, `intentional_repeat_reason`), optional scene titles and uncertainties; **no timestamps**. Code assigns `clip_001...`, builds `plans/selection_plan.json`, resolves it to frames (existing resolver)
  and validates both plans (`validate_selection_plan`, `validate_edit_plan`).
- **Feedback loop**: problems the model can fix (a moment that is unknown, not in the scene's candidates or excluded, a clip longer than its moment, a scene or the total outside tolerance, a repeated moment without a
  reason, a dissolve longer than its clips, invented title text, an overlap on V2, a missing `must_include`) are sent back **once** (`planning.selection_retries`) together with the previous reply. Then, only where the fix is unambiguous,
  code repairs and records: invalid clips are dropped, durations clamped into the moment, dissolves shortened, scene and total durations scaled proportionally within each moment's bounds (never longer than a moment,
  never under 1 s), bad titles dropped. Anything else (a missing scene, too little footage for the target, a `must_include` left out) fails the stage with the findings and `plans/selection_report.json`.
  Scene order always follows the outline. Every repair is listed under "Validation" in `plan_summary.md`.
- **Caching**: the `plan` stage is keyed by the approved outline hash, the digest of its candidate records, the whole request text, prompt and schema versions, capabilities, endpoint/model/params/mode,
  retry and token limits, and the project, editorial, validation and title settings. Edit the outline, change the model, or change `--duration` (a new outline) and it plans again; an unchanged run makes no request.
- **Files** (`plans/`): `selection_plan.json`, `edit_plan.json`, `plan_summary.md`, `validation_report.json`, `selection_report.json` (model, prompts, usage, each attempt's findings and repairs), `timeline_planner_response.schema.json`
  (also in `schemas/`).
- **Cost and privacy** (what leaves the machine, per stage; `status` and the stage summaries show token counts; a `local` endpoint sends nothing off the machine, `media.local_only: true` enforces it):
  `analyze` sends sampled **frames** (images) plus transcript excerpts and base file names to `llm.vision`; `transcribe` sends **audio** to the speech backend only if you configured a remote one; `outline` sends one text request with all
  candidate records (descriptions, local capture times, speech excerpts, base names; no images, no GPS); `plan` sends one text request (a second only when validation finds something) with the outline and the records of its candidate moments only.
  Everything is cached in `llm/` of the workspace (no repeat cost for unchanged requests). Treat descriptions and transcripts as personal data.

## Titles

Titles are simple, editable text on track V2 (DESIGN §13.4). They live in the plans (`selection_plan.json` / `edit_plan.json`, schema 1.1: `titles[]` with `title_id`, `text`, `role`, `scene_id`, `start`, `duration_seconds`,
`track: V2`, `style: simple`, and in the edit plan the exact frames), are placed in the FilmCraft project as text clips, read back and checked, and listed in QC (`titles`) and the handoff report.

- **Off by default.** `editorial.opening_title: true` adds an opening title with `project.title` (your text, never invented); `editorial.closing_title: "..."` adds a closing title with exactly that text at the end;
  `editorial.scene_titles: true` lets the **LLM planner** show the outline's scene titles (it may only copy the outline's title of the scene; any other text, such as a place or date, is rejected). `editorial.title_seconds`
  (default 3) is the on-screen time of the opening and closing titles, `editorial.max_titles` (default 6) the cap. The baseline planner supports the opening and closing titles too.
- **Rules**: one line, at most 60 characters (FilmCraft names a text clip after its first line, so a two-line title would fail the read-back), 1 to 10 seconds, inside the sequence, no two titles overlapping,
  none overlapping an overlay clip on V2. A title whose text equals a folder name of its clips is accepted but flagged "from folder name" in the plan's uncertainties. In `edit_plan.json` you may type your own title text (`text_source: "user"`); it is
  checked for structure, not origin; remember to update `plan_hash`.
- **Position/size**: lower-left, sized from the sequence height (the adapter's defaults); change them in FilmCraft.
- **Resolve**: no interchange format carries titles, so `handoff_report.md` lists them. `trip-vlog resolve-finish` recreates them from the same timeline spec as editable Text+ titles on V2 at the same frames
  (experimental, Studio only), verified by read-back, and lists any it cannot place. Run `resolve-finish --dry-run` first to see what it would do.
- **Older plans** (schema 1.0) still load; they simply have no titles.

## Speech transcription (optional)

Phase 3c adds two stages after `segment`: `audio` (cheap measurements, always on) and `transcribe` (optional, **off until you choose a backend**). Both run inside
`trip-vlog transcribe` and, before labeling, inside `trip-vlog analyze`; `draft --planner baseline` never runs them. Nothing is tied to one vendor: transcription is
a small adapter with three backends.

```yaml
speech:
  enabled: true                  # the one switch (media.transcribe_speech is a deprecated alias, see below)
  backend: none                  # none | openai_transcriptions | command
  language: null                 # e.g. "en"; null lets the backend detect it
  min_speech_seconds: 1.0        # skip assets with less non-silent audio than this (the segmenter's silence detection)
  audio_format: wav              # wav | flac: the temporary 16 kHz mono file handed to the backend
  keep_audio: false              # true keeps media_index/audio/<asset>/... instead of deleting it after use
  chunk_seconds: 600             # HTTP backend only: longer audio is uploaded in chunks of this length (25 MB limits, retries)
  context_max_chars: 1200        # transcript text per segment shown to the vision model in `analyze`; 0 = never send transcripts there
  endpoint:                      # backend: openai_transcriptions
    base_url: https://api.openai.com
    path: null                   # null -> /v1/audio/transcriptions
    model: null                  # or the env var named by model_env (default TRIP_VLOG_SPEECH_MODEL); no model id is built in
    model_env: TRIP_VLOG_SPEECH_MODEL
    api_key_env: OPENAI_API_KEY  # NAME of the environment variable holding the key; null = no Authorization header
    headers: {}                  # values may use ${ENV_VAR}; never logged
    timeout_seconds: 300
    max_retries: 2               # connection errors, 429 and 5xx, with backoff / Retry-After; a timeout is not retried
    response_format: verbose_json   # verbose_json | json | text | srt | vtt
    timestamp_granularities: [segment]   # sent only with verbose_json; [] omits the field
    params: {}                   # extra form fields (temperature, prompt, ...); scalars or lists
    allow_insecure_http: false   # plain http to a non-loopback host needs this
  command:                       # backend: command
    executable: null
    args: []                     # must contain {input} and {output_dir} (or {output_base})
    output_format: srt           # srt | vtt | json
    timeout_seconds: 3600
```

**Backends**

- `none` (default): the stage reports `skipped` ("speech.backend is none") and is not an error; `status` does not list it as unfinished. Audio features are still measured.
- `openai_transcriptions`: `POST {base_url}{path}` as `multipart/form-data` (standard library only, no SDK) with the fields `file`, `model`, `response_format`
  (`verbose_json`), `timestamp_granularities[]` (`segment`), optional `language`, plus your `params`; `Authorization: Bearer <key from api_key_env>`. The file is always sent
  as `audio.wav`/`audio.flac`, never under its real name. Replies are read as `segments[] {start, end, text, avg_logprob, no_speech_prob}` and `language`; a server that
  returns only `text` yields one segment spanning the clip, marked `timestamps: coarse`. **The request shape follows the public OpenAI documentation; verify it against your
  server** (`trip-vlog llm-check --speech`, below). OpenAI-compatible local servers (faster-whisper-server, a whisper.cpp server, LocalAI, ...) work at `http://127.0.0.1:<port>`.
- `command`: runs a local program with an argument list (no shell). Placeholders inside each argument: `{input}` (the WAV/FLAC), `{output_dir}` (an empty temporary folder,
  removed afterwards), `{output_base}` (`{output_dir}/transcript`, for tools that take a prefix) and `{language}` (the configured language or `auto`). After the program exits 0 the
  file `transcript.<ext>` (or the only `*.<ext>` in `{output_dir}`) is read as SRT, VTT or whisper-style JSON (openai-whisper / faster-whisper `segments[]`, whisper.cpp `-oj`).
  A non-zero exit, a timeout or a missing output file fails that asset with the tool's last output.

**Examples**

```yaml
# OpenAI (audio leaves the machine): export OPENAI_API_KEY first
speech:
  backend: openai_transcriptions
  endpoint: {model: <your transcription model id>}
```
```yaml
# A local whisper server on this machine (faster-whisper-server, whisper.cpp server, LocalAI): no key, works with media.local_only: true
speech:
  backend: openai_transcriptions
  endpoint: {base_url: "http://127.0.0.1:8000", api_key_env: null, model: <the model name your server expects>}
```
```yaml
# whisper.cpp command line tool (model path and executable are yours). -osrt -of <prefix> writes <prefix>.srt
speech:
  backend: command
  language: en
  command:
    executable: /opt/whisper.cpp/build/bin/whisper-cli
    args: ["-m", "/path/to/your-whisper-model.bin", "-f", "{input}", "-l", "{language}", "-osrt", "-of", "{output_base}"]
    output_format: srt
```
```powershell
# Windows PowerShell: key for a remote endpoint, then dry-run, check and run
$env:OPENAI_API_KEY = "..."
trip-vlog transcribe --output C:\vlogs\lisbon --media D:\Trips\Lisbon --config .\project.yaml --dry-run
trip-vlog llm-check --config .\project.yaml --speech --no-llm
trip-vlog transcribe --output C:\vlogs\lisbon --media D:\Trips\Lisbon --config .\project.yaml
# whisper.cpp on Windows in project.yaml: executable: 'C:\tools\whisper.cpp\whisper-cli.exe' (single quotes keep the backslashes), args as above
```
```sh
trip-vlog transcribe --output ./vlog --media ./trip --dry-run    # assets, minutes of audio, requests, backend/endpoint, local or remote; extracts and sends nothing
trip-vlog llm-check --config project.yaml --speech --no-llm      # one generated 1-second tone to the backend; empty text is fine
```

**`media.transcribe_speech` vs `speech.enabled`.** `speech.enabled` is the single source of truth. The older `media.transcribe_speech` is still accepted: at load time its value is
copied into `speech.enabled` and the alias is cleared (so the effective `project.yaml` shows only `speech.enabled`). Setting both to different values is a configuration error.

**What is stored.** Transcript segments (verbatim words, start/end seconds in source time, `confidence` = `exp(avg_logprob)` when the backend reports it, `no_speech_prob`,
language, backend, model, `timestamps: segment|coarse`) are in the catalog `transcripts` table and `media_index/transcripts.jsonl`. Each moment's `transcript_refs` lists the
transcript IDs overlapping its source range (also after labeling narrows the range). `speech_summary` stays `null`: spoken words and inferred summaries are kept apart
(DESIGN §7.3), any later summary must be labeled as inferred, and subtitles (§13.4) may only come from the transcript. Segments with `timestamps: coarse` are not suitable for subtitles.

**Skipping and caching.** Assets without an audio stream, and assets whose non-silent audio (from the segmenter's silence detection, `media.silence_threshold_db`) is shorter than
`speech.min_speech_seconds`, are skipped and counted in the summary. Each asset's transcript is cached by its content version plus backend identity (backend, model, language, request
format, granularities and `params`; for `command`: executable name, `args`, format, language) and the audio settings (format, 16 kHz mono, and `chunk_seconds` for the HTTP backend).
URLs, keys, headers, timeouts and retry counts are not part of it, nor are the target duration, segment window or any planning setting: those never re-run transcription.
A connection failure of three assets in a row stops the stage; authentication, model or URL errors (401/403/404) stop it immediately; other per-asset failures leave a `partial`
stage and are retried by the next run.

**In `analyze`.** `transcribe` runs before `analyze`. A video segment that overlaps transcript text gets that text (cut at `speech.context_max_chars`) in its labeling request as an
`untrusted_media_data kind="transcript"` block, with an instruction to use it only to ground `event`; only those segments get a new cache key, segments without speech keep theirs.
The prompt template is unchanged (`segment_labels.v1`). Set `context_max_chars: 0` to keep transcripts out of the vision request. With a remote `llm.vision` endpoint this text
leaves the machine too.

### Audio features

The `audio` stage (`media.audio_features`, default true) makes one ffmpeg pass per asset (cached by content version) and stores `features.audio` on every segment and `audio` /
`audio_tags` on every moment (and in `moments.jsonl`), e.g. `{silent, clipped, wind_likely, active_ratio, mean_db, peak_db, lf_share_db, asset_loudness_lufs, audio_tags}`.
They are measurements and hints, not judgments:

| Flag | Rule | Why |
|---|---|---|
| `silent` | power-mean level of the segment below `media.silence_threshold_db` (default -35 dB) | same rule as the segmenter |
| `clipped` | at least 2% of the 0.25 s windows peak at >= -0.1 dBFS | one full-scale peak in a normalised file does not count; sustained flat tops do |
| `wind_likely` | at least 2 non-silent windows, and at least half of them have the 200 Hz low-passed level within 1 dB of the full-band level (>= ~79% of the power below ~200 Hz) | wind and handling rumble sit almost entirely below 200 Hz; speech, surf, crowds and music keep much more above it (white noise: ~2.5%). A steady engine or deep bass can also trigger it |
| `active_ratio` | share of windows above the silence threshold | energy only, not speech detection |
| `asset_loudness_lufs` | EBU R128 integrated loudness of the whole asset (`null` when silent) | loudness is per asset, not per segment |

`audio_tags` are derived only from those flags: `silent`, `clipped_audio`, `wind_likely`, or `clean_audio` (audible, not clipped, not wind-dominated; it says nothing about *what*
the sound is, which stays an editorial judgment). Audio is measured on a mono downmix at 16 kHz, so correlated stereo reads up to ~3 dB louder than the segmenter's per-channel `audio_mean_db`.

### Privacy

Audio leaves the machine **only** with a remote `openai_transcriptions` endpoint (`trip-vlog transcribe --dry-run` and the stage summary say `remote` and how many minutes). A
loopback endpoint (127.x, `localhost`, `::1`) or the `command` backend keeps it local; with `media.local_only: true` a non-loopback `speech.endpoint` is rejected when the
configuration loads (`command` counts as local). Extracted audio lives in a temporary folder and is deleted unless `keep_audio: true`. Transcripts are personal data: they are
stored in the workspace, and are sent to the vision endpoint in `analyze` unless `context_max_chars: 0` (the same endpoint rules apply). Keys are read from environment variables
and never logged, and error messages echoed by a server are masked.

## Platform notes

Windows 10/11 and macOS 13+ are first-class targets, Linux is for development and CI. The code uses `pathlib` only, argument-list subprocesses (no shell),
UTF-8 with `\n` newlines, atomic `os.replace` writes (with short retries for Windows file locks), extended-length paths for long Windows paths, and
generated file names that avoid Windows-invalid characters and reserved names. CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu, macOS and Windows
with Python 3.11 and 3.13. Only Linux has been exercised locally so far; treat Windows/macOS results as unverified until CI has run.

## Known limitations

- **Not implemented yet:** the QC critic and natural-language revisions (Phases 5-6). `--planner llm` has only been exercised with fake endpoints and the generated fixtures; try it on your own footage before making it the default.
  The baseline planner is chronological with fixed clip lengths, stills at a default duration, no music or narration, hard cuts, and titles only when configured.
- Titles are one line only; their size and position are fixed defaults; `resolve-finish` titles are still experimental.
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
