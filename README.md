# llm-vlog-drafter

Draft a travel vlog from a folder of photos and videos, with an LLM. The full design is in [DESIGN.md](DESIGN.md).

**Status:** Phase 0 (FilmCraft spike, see [docs/phase0-filmcraft-spike.md](docs/phase0-filmcraft-spike.md)), Phase 1 (media inventory) and
**Phase 2 (deterministic end-to-end slice)** are done: `trip-vlog draft` turns a media folder into a reopened FilmCraft project, a rendered preview,
a QC report and a Resolve handoff package, using a baseline planner with no LLM. **Phase 3 and 4 (LLM vision labeling, story outline, LLM clip selection
and titles) are implemented** (`draft --planner llm`, see "LLM draft"); the baseline planner stays the default until the LLM path has been tried on real footage.
The critic and natural-language revisions are not implemented yet.

Originals are never modified. Everything the tool writes goes into a separate *output workspace* that must live outside the media folder.

## Install

Requires Python 3.11+ and FFmpeg 4.3 or newer (`ffmpeg` and `ffprobe` on `PATH`; frame extraction uses the scale filter's `force_divisible_by`, added in 4.3).

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

# Phase 5b: optional LLM critic on a finished draft (see "QC critic"): a review report, or review + localized fixes as a new project version
trip-vlog critic --output /path/to/vlog-project [--mode report|apply]

# Phase 6: change a finished draft with a request in your own words (see "Revisions"): a new plan version, project version, preview and QC run
trip-vlog revise --output /path/to/vlog-project "make the beach scene shorter and drop the blurry sunset clip" [--dry-run] [--file request.txt]
trip-vlog approve --output /path/to/vlog-project --revision    # when revise stopped with exit code 3 (ambiguous or destructive): then `resume` or `revise --apply`

# Accepted a hand edit of plans/edit_plan.json and/or plans/story_outline.json? Re-validate and re-seal it explicitly (see "Hand-editing a plan or outline")
trip-vlog reseal --output /path/to/vlog-project [--plan | --outline]

# Optional, Resolve Studio only: finish the handoff inside Resolve (see "Finish in Resolve" below)
trip-vlog resolve-finish --output /path/to/vlog-project [--dry-run] [--project-name NAME] [--keep-on-failure]
```

- `--auto-approve` (or `workflow.approval: auto_approve`) skips the stop and records `approval.mode = "automatic"` in `manifest.json`.
- Approval is bound to the SHA-256 of the plan content. Changing `--duration`, the media, or the plan file invalidates it and the run stops again.
- `plans/edit_plan.json` is what gets executed. It is re-validated (schema, IDs, source ranges, durations, transitions) at assemble time, so a plan that
  was edited into an invalid state is rejected before FilmCraft is started. If you hand-edit it, run `trip-vlog reseal` (below): it re-validates the edit and recomputes `plan_hash`, which otherwise fails the hash check.
- Re-running on unchanged inputs does nothing: every stage is cached (inputs hash plus a check that its artifact still exists) and `assemble` is a no-op.
  Changing `--duration` re-plans and builds a **new versioned project** (`filmcraft/Trip Vlog Draft_v002.fcproj`); probing and segmentation come from the cache.
  A failed or interrupted assembly leaves its numbered `.fcproj` behind, marked failed (see "Failed builds"), never reused; `resume` builds the next version, so clips are never appended twice.
- `resume` continues the pipeline recorded in `manifest.json`; the media folder is taken from there. Settings persist in `<output>/project.yaml`.

Exit codes: `0` finished (QC warnings and unprobeable files are reported, not fatal), `1` a stage failed, `2` bad configuration/arguments, `3` stopped at the approval gate, `4` `approve` refused (nothing to approve yet, or the plan/outline is invalid), `5` `revise` needs an answer first (nothing was proposed, so there is nothing to approve: ask again with the answer in the request), `130` interrupted.

Configuration: copy [examples/project.yaml](examples/project.yaml), edit, pass with `--config`. Command-line overrides win:
`--duration`, `--aspect`, `--fingerprint sha256`, `--no-recursive`, `--ffprobe PATH`, or any field via `--set media.thumbnail_max_side=320`.
Invalid values and unsafe paths (workspace inside the media folder, outputs escaping the workspace) are rejected with exit code 2.

### Workspace layout

```
vlog-project/
  project.yaml  manifest.json  README.md          # manifest: versions, approval, FilmCraft version, per-stage status
  media_index/    catalog.sqlite (authoritative), assets.jsonl, moments.jsonl, transcripts.jsonl, inventory_report.json, thumbnails/, contact_sheets/, analysis/ (sampled frames + per-segment sheets)
  plans/          edit_plan.json, plan_summary.md, selection_plan.json, *.schema.json, validation_report.json, revisions/ (critic fixes: critic_rNN.json/.md + the parent plan)
  filmcraft/      Trip Vlog Draft_v001.fcproj ..., capability_snapshot.json, command_log.jsonl, execution_journal.json, mapping_report.json
  preview/        draft.mp4, qc_report.json/.md, contact_sheet_timeline.jpg (+ _02, _03 ... for many cuts), render_report.json
  qc/             preview_sheet.json (index of the preview sheets: tile -> record time, clip, scene, side); critic_report.json/.md (only with qc.critic on)
  resolve_handoff/  timeline.xml, timeline.otio, media_manifest.csv, edit_plan.json, project_reference.json, handoff_report.md, compatibility_report.json/.md,
                    timeline.srt + captions_report.json (only with captions.enabled), resolve_finish_report.json/.md (only after `resolve-finish`)
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

Further deterministic checks (Phase 5a, `qc:` in `project.yaml`; they only ever **warn**, a draft fails only for objective defects such as offline media,
a render or decode failure, gaps, or a duration out of tolerance):

- `preview_decodes`: ffmpeg decodes the whole preview (video and audio) without error.
- `speech_truncation`: when transcripts exist, a clip's source in/out point falls inside a transcript segment by more than `qc.speech_cut_tolerance_seconds`
  (0.25). Names the clip and the (estimated, segment-level) words cut; muted clips and edges that continue the same take are ignored.
- `audio_discontinuity`: at a cut between two clips that keep their source audio, both audibly active (`qc.audio_active_ratio_min`, 0.5) and whose mean levels
  (per-moment measurement, plus the constant `ambient_low` gain) differ by at least `qc.audio_jump_db` (12 dB).
- `music_vs_speech`: a music clip over a clip with speech (transcript) or active natural sound, unless the plan's own gains put the music at least
  `-qc.music_max_gain_db` (6 dB) below it. No ducking is generated, so `duck_music_under_speech` does not count.
- `scene_order`: the plan's scenes follow the approved story outline's order (LLM planner only; skipped otherwise).
- Checks that cannot run (no transcripts, no audio measurements, no music, no outline) report status `skipped` (with `data.skipped: true` and the reason). `skipped` is counted separately and does not warn or fail; it is not a pass.

**Preview sheets.** `preview/contact_sheet_timeline.jpg` shows, for every cut, one frame just before (`qc.cut_sample_offset_seconds`, 0.15 s, clamped inside the
clip) and one just after it, plus the first and last frame; every tile is labelled with record time, scene ID and clip_instance_id. `qc.contact_sheet_max_frames`
(48) is the number of tiles per sheet: more cuts make further sheets (`..._02.jpg`), at most `qc.contact_sheet_max_sheets` (8), beyond which cuts are sampled
evenly and the index says so. `qc/preview_sheet.json` (`outputs.qc_dir`) maps every tile id to `record_seconds`, `clip_instance_id`, `scene_id`, `side`
(`start|before|after|end`) and its place on its sheet, and lists the cuts with their two tiles (read it with `trip_vlog.qc.preview.load_preview_sheet_index`).
`qc_report.json` also has a `critic` block (`not_run`): the seam for a later LLM reviewer, which would read the index and the checks.

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

### Hand-editing a plan or outline (`reseal`)

`plans/edit_plan.json` and `plans/story_outline.json` are meant to be edited by hand. The edit plan carries a `plan_hash` that must match its content, and `approve` and the
assemble step re-check it, so an edited plan is refused (`plan_hash_mismatch`) until it is **re-sealed**:

```
trip-vlog reseal --output /path/to/vlog-project            # whichever of the outline and the plan exist
trip-vlog reseal --output /path/to/vlog-project --plan     # or --outline
trip-vlog approve --output /path/to/vlog-project --plan    # reseal never approves anything
trip-vlog resume  --output /path/to/vlog-project
```

- **It runs the full validators on the file as it is**: for the plan, the same ones that run before assembly (schema, asset and moment references, source ranges, transitions and handles, titles,
  `must_include` / `must_exclude`, the duration tolerance against the current target, with the target and waived references of accepted revisions applied); for the outline, the outline validator (moments, durations, coverage).
  Any error refuses the seal with the findings (exit code 4), and **nothing is changed**. Frame-boundary repairs the validators make (a source time that is not on a frame) are applied and listed.
- **It keeps a backup** of the file as it was, `plans/edit_plan.<hash>.bak.json` / `plans/story_outline.<hash>.bak.json` (the names the plan and outline stages use when they replace an edited file; an existing backup with other bytes is never overwritten).
- **It writes the sealed file**: the plan with `plan_hash` recomputed (UTF-8, no byte order mark; files saved with a BOM by Windows editors are read fine), the outline in its canonical form. It refreshes what is derived from it
  (`plan_summary.md` and `validation_report.json`, or `story_outline.md`) and records the seal in `manifest.json` (`reseal`) and in `plans/reseal_report.json` (`reseal_outline_report.json`). Running it again on a sealed file does nothing.
- **Approval is not touched in your favour.** Approval is bound to the hash of the content, exactly as for any hand edit: if the content changed, an earlier approval is marked `invalidated` (what the approval stage would do on the next run) and you approve again with
  `trip-vlog approve`; if only the `plan_hash` field was stale, the content hash did not change and an existing approval still applies. `reseal` reports what it found: `none` (no approval recorded), `kept`, `invalidated`, or `for another plan` / `for another outline` (the recorded approval is bound to some other content; it is left as it is). A sealed plan never qualifies for the automatic approval "derived from the approved outline" (a hand edit needs its own approval), and `workflow.approval: auto_approve` still approves whatever is on disk, as it always did.
- **Plans from critic and revision rounds** are ordinary plans and are re-sealed the same way; the round's record stays as it was, and the handoff report says the plan was re-sealed. Their pending proposals become stale because the plan changed.
- **Critic and revise after a reseal that changes clips.** `trip-vlog critic` and `trip-vlog revise` only work on a plan that its selection plan resolves to exactly. After a reseal that changes clips (not a note or other wording), they refuse the plan ("the current plan differs from what its selection plan resolves to") until it is re-planned; `reseal` prints this when it applies, with the reason. The sealed plan itself is still approved, built and handed off as usual. `reseal` refuses while a critic or revision round is half-applied (run `resume` first).
- **After resealing an outline**, the plan made from the old outline is not kept in step: once you approve the outline `resume` makes a new plan from it (an edited plan is first copied to `edit_plan.<hash>.bak.json`).
- It never touches media, FilmCraft projects or Resolve projects. A build from the earlier plan is kept; after approval `resume` builds a new project version (or reuses the current one when the timeline is identical).

### Failed builds

When a FilmCraft build fails midway (a command error, a failed read-back or reopen check) or is cut off (the process was killed), its numbered `filmcraft/<Name>_vNNN.fcproj` is **kept** and marked in `filmcraft/execution_journal.json`
(`status: failed`; a run found still `running` by the next build is marked `failed` and `abandoned`). `manifest.json` lists them under `filmcraft.failed_versions`, `trip-vlog status` prints them, and the handoff report names them.
A failed version is never used: `resume`, preview, QC, the critic, revisions and the handoff only work on the newest build that completed and was verified after reopening. The next successful build always takes a **new** version number:
numbers are reserved by the journal and by the files on disk (the `.fcproj` and its `.timeline_spec.json`), so a failed version is never overwritten or reused, even if the journal is lost. Nothing is deleted: failed projects
are left for you to inspect or remove by hand. Original media and Resolve projects are never touched.

**Two processes on one workspace.** Each run records the pid, host and start time of the process building it, and the journal is only updated under a short lock file (`filmcraft/execution_journal.lock`, reloaded before every write), so concurrent processes do not overwrite each other. A run still `running` is marked abandoned only when its process is gone (same host, pid not alive), or when it was started on another host more than 24 hours ago; a build running in a live process is never touched, and a second build is refused at once with a message naming it (run the command again when it has finished). A lock left by a dead process is cleared automatically. A re-run with the same spec and media is a no-op against the newest completed build even if a later attempt failed.

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
      max_retries: 2                 # transport retries on 429/5xx/connection errors (backoff + Retry-After up to 60 s; a longer Retry-After stops retrying and is reported)
      structured_output: native      # native | json_prompt | none
      max_tokens: 4096               # cap on a request's max_tokens
      max_tokens_field: null         # body field for the token limit; null = adapter default (see below)
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
  servers here; verify against yours and adapt with `path`, `params`, `max_tokens_field` and `structured_output`. `params` may not set fields the
  gateway controls: `model`, `messages`, `input`, `system`, `instructions`, `stream`, `response_format`, `max_tokens`, `max_completion_tokens`,
  `max_output_tokens`, the profile's `max_tokens_field`, or a `format` inside `text` / `output_config` (use `structured_output`); such a config is rejected
  naming the key, as is a value that is not plain data (quote YAML dates). The token-limit field defaults to `max_tokens` (`openai_chat`, `anthropic_messages`)
  or `max_output_tokens` (`openai_responses`), except that `openai_chat` against `api.openai.com` sends `max_completion_tokens`, which current OpenAI
  reasoning models require; other OpenAI-compatible servers keep `max_tokens`. Set `max_tokens_field` to override either way. When a server answers 400 and
  mentions the token field, the error says which field was sent and points at `max_tokens_field`.
- **Structured output**: `native` sends the provider's JSON-schema option (a sanitized copy: `additionalProperties: false`, no `minimum`/`maxLength`/...; OpenAI
  strict mode also wants every property listed in `required`). `json_prompt` sends no schema parameter and instead appends the schema to the prompt (most local
  servers need this); `none` sends nothing. In every mode the gateway extracts the first JSON value from the reply (fences and prose tolerated), validates it
  against the full schema, and on failure re-asks with the validation errors only (`llm.schema_retries`, default 2; no extra media is re-sent).
  Refusals, content-filter stops and `max_tokens` truncation raise typed errors (`LLMRefusalError`, `LLMTruncatedError`); output is never silently cut.
- **Local-only**: an endpoint whose `base_url` host is loopback (`127.x`, `localhost`, `::1`) is "local". `media.local_only: true` requires the profiles used by
  `llm.text` and `llm.vision` to be local (checked when the config loads). Each call's log record includes `endpoint` and `local`.
- **Response cache**: `<workspace>/cache/llm/<fingerprint>.json` (`llm.cache_dir`). The fingerprint is a SHA-256 over the prompt template id + version, system text,
  schema, text parts, each image's *source file* hash plus the preparation settings (`image_max_side`, `image_quality`, prep version; not the re-encoded bytes,
  which can differ across OSes), model id, `max_tokens`, effort, and the profile's `api`, `params`, `structured_output`, `max_tokens` cap and token field. Frames cut from video are named in the fingerprint by the asset content
  version, frame number and sampling settings rather than their (ffmpeg-build dependent) JPEG bytes; photos hash the original file. It excludes `base_url`, `path`, headers, timeouts and keys,
  so a recording made against one host replays against another. A changed frame, prompt version, schema, model or params is a miss, and so is a changed `content_version` (a re-copied or touched file is re-labelled, even if its pixels are identical); everything else is a hit with no call.
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
trip-vlog llm-check --output ./vlog                  # every endpoint used by llm.text / llm.vision (and llm.critic / llm.revise when set): a text request, then an image request
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
  nothing is reported under `missing_coverage`. References are paths from your own folder layout, so requests never contain them: the model is told "required item N"
  together with the moment IDs it resolves to, and that N is spelled out only in the local reports. `preferred_topics` is free text and is passed to the story editor as a preference.
- **Validation and repair** (deterministic, `planning/outline.py`). Repaired and recorded in the report (and shown under "Checks" in the summary): unknown moment IDs, moments not offered
  to the model, duplicates (a moment is used by one scene; the hook moment may appear once more in its chronological scene), moments matching `must_exclude`, an opening hook longer
  than `editorial.opening_hook_seconds` (shortened), scene budgets outside `project.duration_tolerance_pct`/`_seconds` of the target (all non-hook scenes scaled proportionally to the
  exact target), hook fields left over when no hook is used, scene IDs (`scene_01`, ... assigned by code). Reported as warnings: `story_mode_used` differing from `project.story_mode`,
  an out-of-order scene when `story_mode: chronological`, a `must_include` that matches nothing (also added to `missing_coverage`). Everything that would change the story is an **error** instead: a scene left without moments,
  a hook that is not allowed (`allow_nonlinear_hook: false`) or whose scene/moment is inconsistent, an uncovered `must_include`. Errors are sent back to the model **once**
  (`planning.outline_retries`) with the previous reply; if they remain the stage fails with the findings and writes `plans/story_outline_report.json`. Schema violations are retried by the
  gateway first (`llm.schema_retries`). Text length and number ranges are not part of the reply schema (providers ignore them and one long sentence must not fail the stage): over-long texts are cut and
  out-of-range budgets clamped by code, and each change is a recorded repair. When a stage fails, its cached replies are removed, so `resume` asks again.
- **Your edits are checked, not rewritten**: when you edit `story_outline.json`, `resume` re-validates it without the LLM (any usable moment may be used, not only those that fit in the
  prompt; budgets off target are a warning; blanks around moment IDs are ignored; a UTF-8 byte order mark from Windows editors is fine). Errors fail the `outline_approval` stage and `approve` refuses; fix the file and run `resume`.
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

- **Stages**: `scan, probe, catalog, thumbnails, segment, audio, transcribe, analyze, outline, outline_approval, plan, approval, assemble, preview, qc, critic (only with `qc.critic` on), handoff`. `plan` is the LLM selection.
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
  retry and token limits, the project, editorial, validation and title settings, and the catalog fields the resolver reads for those moments (ranges, best sub-range, length, frame rate, audio, version).
  Edit the outline, change the model, change `--duration` (a new outline) or re-index changed media and it plans again; an unchanged run makes no request.
  A plan stage that replaces an `edit_plan.json` you edited by hand first keeps a copy as `plans/edit_plan.<hash>.bak.json`. A plan stage that **fails because the plan was refused** (validation or selection failure; not a timeout, 401/429/5xx or a missing prerequisite, which leave the earlier plan alone) moves the earlier plan files aside
  (`edit_plan.stale.json`, `selection_plan.stale.json`, `plan_summary.stale.md`), so nothing that looks current is left next to the failure report; the next successful plan writes the normal names again. A rename that fails (a file locked on Windows) is listed as `stale_rename_failed` in the stage summary.
- **Files** (`plans/`): `selection_plan.json`, `edit_plan.json`, `plan_summary.md`, `validation_report.json`, `selection_report.json` (model, prompts, usage, each attempt's findings and repairs), `timeline_planner_response.schema.json`
  (also in `schemas/`).
- **Cost and privacy** (what leaves the machine, per stage; `status` and the stage summaries show token counts; a `local` endpoint sends nothing off the machine, `media.local_only: true` enforces it):
  `analyze` sends sampled **frames** (images) plus transcript excerpts and base file names to `llm.vision`; `transcribe` sends **audio** to the speech backend only if you configured a remote one; `outline` sends one text request with all
  candidate records (descriptions, local capture times, speech excerpts, base names; no images, no GPS); `plan` sends one text request (a second only when validation finds something) with the outline and the records of its candidate moments only.
  Everything is cached in `llm/` of the workspace (no repeat cost for unchanged requests). Treat descriptions and transcripts as personal data.

## QC critic (optional)

An LLM can review the finished draft the way a second editor would (DESIGN §9.4, §14 steps 6-8): story clarity, repetition, pacing, awkward cut points, orientation, audio conflicts, missing
context. It is **off by default**, advisory, and never fails a draft. Deterministic QC stays the authority on objective defects; the critic's findings are kept apart (`qc/critic_report.*`, and the
`critic` block of `preview/qc_report.json`) and never change the QC status.

```sh
trip-vlog draft ... --set qc.critic=report        # review as part of a draft (stage `critic`, after `qc`, before `handoff`)
trip-vlog critic --output ./vlog                  # on a finished draft, this run only (project.yaml is not rewritten); default mode: report
trip-vlog critic --output ./vlog --mode apply     # also apply the proposals that validate and are local
#   read ./vlog/qc/critic_report.md, and with apply ./vlog/plans/revisions/critic_r01.md
```

- **Modes** (`qc.critic`): `off`; `report` (writes `qc/critic_report.json` / `.md`: findings, the evidence checked, each proposed change with its verdict, and the diff of what *would* change); `apply` (as `report`, then
  applies the proposals described below). Write `"off"` in quotes in YAML (an unquoted `off` is the boolean false; it is accepted as off). Limits: `qc.critic_max_rounds` (1), `qc.critic_max_ops_per_round` (3),
  `qc.critic_apply_min_severity` (`major`; `minor` also applies minor findings), `qc.critic_images` (true), `qc.critic_max_sheets` (4), `qc.critic_max_tokens` (6000).
- **Endpoint**: purpose `critic` like `text` and `vision`: `llm.critic: <profile>`. Unset, it uses the `vision` profile (it reads preview sheets), or the `text` profile when no image is sent. The model comes from the profile
  as always (`model` / `model_env`; no built-in default); keys from the environment variable the profile names. `media.local_only` covers it when the critic is on; `llm-check` checks its profile.
- **What it is given** (`critic.v2` + `critic_input.v1`): the compact edit plan (scenes; clips with ids, record and source times, durations, usable length of the moment, transitions, sound policy, orientation; titles), the
  deterministic QC results with their data, the per-clip audio measurements, the tile index (tile id, sheet, record time, cut side, scene, clip) and the **preview sheet images** themselves
  (`qc.critic_max_sheets` of them, spread over the film). Everything derived from the media (scene wording, descriptions, titles, QC details that can quote speech) is wrapped as untrusted data. With
  `qc.critic_images: false`, an endpoint with `supports_images: false`, or no sheets (no ffmpeg, preview not rendered), it runs **text only** and the report says so; it then cannot cite tiles. It never gets file
  names, folders, GPS or the raw media. What leaves the machine: that text plus up to four JPEG sheets of low-resolution preview frames, to the `critic` endpoint (`local` / `remote` is in the report and in `status`).
- **Response** (`critic_response`, strict schema, also in `schemas/`): `findings[]` with `severity` (`info|minor|major`), `category` (`story_clarity, repetition, pacing, cut_point, orientation, audio_conflict, missing_context, other`),
  affected `scene_ids` / `clip_instance_ids`, `evidence` and an optional `proposed_change` from a **closed set**: `trim_clip` (new duration in seconds and/or anchor, within the moment), `swap_moment` (another candidate
  moment of the same scene from the approved outline), `remove_clip`, `move_clip` (inside its scene), `set_transition` (cut, or a dissolve of 0.1-5 s), `set_clip_audio` (`mute`, `keep`, `lower` = the configured ambient gain) and
  `shorten_scene`. The model writes no timestamps, only durations in seconds; code converts to frames and the existing resolver recomputes the source ranges.
- **Checked in code**: evidence must name something supplied (a tile whose image was sent, a QC check, a clip or scene of the plan, a clip with audio measurements); other evidence is dropped, and a finding left with none is dropped
  and listed ("do not claim to have heard or seen content not supplied"). Unknown ids are removed. A proposal is parsed, tried through the resolver and `validate_selection_plan` / `validate_edit_plan`
  (must_include / must_exclude, total-duration tolerance, transitions and handles, titles, repeats) and judged: a malformed, out-of-bounds or invalid one stays on its finding as a note (`change.status: invalid`) and changes nothing.
  A dissolve the editor cannot place (it would need a non-centred alignment, which the FilmCraft adapter places as a hard cut) is refused rather than silently weakened.
- **Applying** (`apply`): only proposals of findings at least `qc.critic_apply_min_severity` that cite evidence about the clip or scene they change (a tile of that clip, its plan entry, its audio, or a check whose data names it; otherwise `change.status: unsupported`) and are **local** (the change is in a scene or clip the finding names, or next to it; a finding spans at most two neighbouring scenes
  and then only adjacent clips), most severe first, each tried on top of the earlier ones, at most `qc.critic_max_ops_per_round`. The result is a **new** edit plan (`plans/edit_plan.json`; the parent plan, selection plan, summary and QC report are kept under
  `plans/revisions/critic_rNN.*`), the diff in `plans/revisions/critic_rNN.json` / `.md` (scene and clip level, duration change per scene and in total), a new versioned FilmCraft project
  (`filmcraft/Trip Vlog Draft_v00N.fcproj`; the earlier one is never touched), a re-rendered preview (the previous one stays as `preview/draft.before_critic_rNN.mp4`), a re-run QC, and the round recorded in `manifest.json`
  (`critic.rounds`). If the new project cannot be built, the previous plan and approval are put back and the proposed plan stays in `plans/revisions/` (stage `partial`; the same revision is not tried again on `resume`, a different one is). After the last round the final version is reviewed once
  more (report only), so the report always describes what `handoff` packages.
  Safety rails: the round limit counts the applied rounds of the plan's lineage (and of the run), and a change that would return the plan to a version an earlier round started from or produced is not applied. Nothing is
  reverted when QC gets worse, but no further round runs once the new version has a failing check the old one did not, or its preview or QC could not be produced; the report, `status`/`draft` output and `handoff_report.md`
  say so (`trip-vlog critic` lifts the stop). Before the first file is touched the round re-checks that `edit_plan.json` is still the reviewed plan. A crash or Ctrl-C mid-round leaves a marker (`critic.applying`);
  the next run puts the saved parent files back (plans, QC and mapping reports, preview; files that did not exist before are deleted). A rebuild that fails for the environment (FilmCraft missing, disk full, timeout) is not
  remembered and is retried by `resume`. `status` and `handoff_report.md` name the latest project version and the critic report.
- **Approval**: a fix starts only from an approved plan. It is recorded as `approval.mode = "critic_fix"`, bound to the **new** plan hash, with `parent_plan_hash` (and the `parent_approval` it replaced), the revision file listing exactly the operations
  applied, the report snapshot `qc/critic_report.rNN.json` that motivated it and its sha256, and `authorization: automatic`. It authorizes those validated operations and nothing else; any later edit changes the hash and needs the usual approval.
  With `workflow.plan_review: true` nothing is applied automatically: the stage writes the proposed revision (`status: proposed`), stops (exit 3, `awaiting_approval`), and you read `plans/revisions/critic_rNN.md`, then
  `trip-vlog approve --critic` and `trip-vlog resume` apply exactly that revision (`authorization: manual`). `approve --critic` refuses when the plan has changed since. An approved revision is applied by `resume` even when `qc.critic` is `off` in `project.yaml` by then.
- **Caching**: keyed by the plan hash, a digest of the QC results, a digest of the sheet index, the whole request text, the images sent, prompt and schema versions, endpoint / model / parameters / mode, the validation and planning settings, the minimum still length, the failed-revision list, the mode and limits and the
  rounds already applied. Unchanged inputs make no request (the stage is a cache hit; a repeat of the same request is answered by the workspace response cache); a failed or unusable reply is retried by `resume`. A plan made by
  a critic round is not re-reviewed beyond `qc.critic_max_rounds`. A plan stage that re-plans keeps the critic-fixed plan as a backup (`edit_plan.<hash>.bak.json`) like any hand-edited plan.
- Changes apply only to a plan that still resolves to itself (a hand-edited `edit_plan.json`, or a catalog changed since, is reviewed but not changed). `swap_moment` needs the approved outline of `--planner llm`; a baseline plan has none.

## Revisions (`revise`)

Change a finished draft by saying what you want (DESIGN §16): "make the beach scene shorter and drop the blurry sunset clip", "shorten the restaurant section by 12 seconds", "keep the natural sound from the
waterfall", "move the skydiving scene closer to the beginning", "use fewer similar beach shots". The draft must be approved and built (`draft` finished); media analysis is **not** re-run.

```sh
trip-vlog revise --output ./vlog "make the beach scene shorter and drop the blurry sunset clip"
trip-vlog revise --output ./vlog --file request.txt --dry-run     # ask the model and show the diff; nothing is changed
trip-vlog revise --output ./vlog "..."                            # exit 3: proposed, not applied (see "Confirmation"); read ./vlog/plans/revisions/revise_r01.md
trip-vlog approve --output ./vlog --revision && trip-vlog resume --output ./vlog      # or: trip-vlog revise --output ./vlog --apply
trip-vlog revise --output ./vlog --reset-overrides                # forget a target length / waived must_include requirement that earlier revisions set (see "Length and requirements move on purpose")
```

- **How it works** (stage `revise`, after `qc`): a model (purpose `revise`: `llm.revise: <profile>`, else the `text` profile; prompt `revise.v1` + `revise_input.v1`; strict schema `revise_response`, also in `schemas/`) maps your request onto the
  **closed set of plan operations** of `planning/plan_diff.py` (the critic's `trim_clip`, `swap_moment`, `remove_clip`, `move_clip`, `set_transition`, `set_clip_audio`, `shorten_scene`, and for revisions `add_moment`, `remove_scene`,
  `move_scene`), naming clips and scenes by the ids of a compact plan summary it is sent (scenes and clips with ids, record and source times, durations, usable length of each moment, transitions, sound, titles, and the outline's
  alternative moments for `swap_moment` / `add_moment`). It never edits plan JSON and writes no timestamps, only durations in seconds. Each operation carries a rationale and the words of the request it answers; the reply also lists
  `ambiguous` questions, a `destructive` flag and the parts of the request no operation can do (`unsupported`: titles, music, colour, speed are not changed by revisions). Your request is the only instruction the model follows;
  text derived from the media (scene wording, clip descriptions, titles) is wrapped as untrusted data, and the request itself sits between fixed markers. Nothing else leaves the machine: no media, no file names, no GPS, no images.
  `media.local_only` covers the `revise` profile.
- **Checked in code**: every operation must parse and name ids that exist; it is then tried on the real plan through the resolver and the validators (must_exclude, titles, handles, repeats, the moment's usable length) and **refused with a
  reason** if the plan would no longer validate (the plan stays as it was and the other operations still get their turn); operations apply in order, each on top of the earlier ones. The film may change length by at most
  `revise.max_duration_change_pct` (50%) of what it is now. At most `revise.max_ops` (12) operations are kept. `swap_moment` / `add_moment` need the approved outline of `--planner llm` (a baseline plan has none). A plan
  that no longer resolves to itself (hand-edited `edit_plan.json`) is not changed.
- **Confirmation**: a revision is applied on its own only when it is clear and not destructive; otherwise it is written as `plans/revisions/revise_rNN.json` / `.md` with status `proposed`, recorded in `manifest.revise.pending`, and the
  run stops (exit code **3**, nothing changed). It needs confirmation when: the model asks a question (`ambiguous`) or flags it destructive; it **removes a whole scene** (`revise.confirm_scene_removal`, true); it removes **more than
  `revise.confirm_removed_clips`** (3) clips; it changes the length of the film by more than `revise.confirm_duration_change_pct` (15%) **and** at least `revise.confirm_duration_change_min_seconds` (10 s), or by more than `revise.confirm_change_pct_any` (30%) whatever the seconds (a 43% cut of an 18 s film is not small); it would move the film's target duration or waive a `must_include` requirement (always); some of the requested changes were refused (only part of the request would be applied: the proposal lists the refused ones, and approving applies exactly the valid ones); it removes content that
  `editorial.must_include` requires (the summary says "required item N", never the reference); the request needed more than `revise.max_ops` operations; `workflow.plan_review: true`; or `revise.auto_apply: false`. Code decides this from the
  result of the operations, whatever the model says. A request that yields no operation but a question stops with exit **5** (nothing is proposed, so there is nothing to approve: ask again with the answer in the request). `trip-vlog approve --revision` records the approval, bound
  to that proposal and to the plan it was made for (refused when the plan changed since); `resume` or `revise --apply` then applies **exactly** those operations (no new request to the model) and refuses, changing nothing, if they no longer give
  the approved plan. A new request replaces an open proposal (the old files stay, marked `superseded`). Removing must_include content or a scene is only possible this way.
- **Result**: a **new** edit plan (`plans/edit_plan.json`; the parent plan, selection plan, summary, QC and mapping reports are kept as `plans/revisions/revise_rNN.parent_*`), approval `mode: revision` bound to the new hash with `parent_plan_hash`,
  the revision file and `authorization` (`automatic` or `manual`), a **new** versioned FilmCraft project (`Trip Vlog Draft_v00N.fcproj`; earlier versions and your Resolve projects are never touched or deleted; original media is never modified),
  a re-rendered preview (the previous one stays as `preview/draft.before_revise_rNN.mp4`), a re-run QC and handoff. QC is **not reverted** when it gets worse; the summary, `status` and `handoff_report.md` say so. If the project cannot be built, the previous plan
  and approval are put back. A crash or Ctrl-C mid-way leaves a marker (`revise.applying`); the next run restores the saved parent files first (same machinery as the critic's rounds, `revision_common.py`).
- **Change summary** (`plans/revisions/revise_rNN.md` + `.json`, printed by the command, referenced in `handoff_report.md`): the request and what the model understood, each change with its reason and the words it answers, the refused ones and why, per scene
  and clip what changed, the length before and after (and the new target), new warnings, and QC before and after. `trip-vlog status` lists the revision history.
- **Length and requirements move on purpose**: when a revision takes the total outside the tolerance of the target duration, the target becomes the new total, and must_include references whose clips a confirmed revision removed are waived. Both are
  recorded in `manifest.revise.overrides` (valid while the plan stage's plan is the one they were made on; whenever the plan stage runs again, even when it makes the same plan, the overrides and any open proposal are dropped and the rules start from `project.yaml` again), not in `project.yaml`. `status` lists active overrides; `revise --reset-overrides` clears them by hand (nothing else is touched).
  QC may warn `scene_order` after `move_scene` (the plan no longer follows the approved outline's order); that is expected.
- **Repeating a command**: the same request that produced the current plan is not applied twice (`--again` overrides); a failed or unusable model reply is never cached, so the request asks again. Unchanged inputs make no request (the response cache answers a repeat).
  Stage cache key: the request text, the plan hash, the whole request sent (plan summary, alternatives, limits), prompt and schema versions, endpoint / model / parameters / mode, `revise.*`, the validation and planning settings with the revision overrides,
  and the pending proposal.
- **Dry runs** always ask (the response cache answers an identical request) and always print the diff; they never write a plan, project or record of a revision.
- **Dropped transitions**: when an operation makes the resolver turn a dissolve into a cut (a moved scene or clip now starts the film, handle too short), the change summary says "transition dropped".
- **After a revision that made QC worse** the critic only reports for that plan (it does not stack automatic rounds on it); revision numbers grow past `r99` (`r100`, ...).
- **Exit codes**: 0 applied (or `--dry-run` shown), 1 nothing could be changed (every operation refused, the model unreachable, a stale or rejected proposal), 2 usage or configuration error, 3 proposed (waiting for `approve --revision`), 5 needs clarification (rephrase the request; nothing to approve). An approved proposal whose rebuild is rejected as a plan is marked stale and is not retried by `resume`; a failure of the environment is retried.

## Titles

Titles are simple, editable text on track V2 (DESIGN §13.4). They live in the plans (`selection_plan.json` / `edit_plan.json`, schema 1.1: `titles[]` with `title_id`, `text`, `role`, `scene_id`, `start`, `duration_seconds`,
`track: V2`, `style: simple`, and in the edit plan the exact frames), are placed in the FilmCraft project as text clips, read back and checked, and listed in QC (`titles`) and the handoff report.

- **Off by default.** `editorial.opening_title: true` adds an opening title with `project.title` (your text, never invented); `editorial.closing_title: "..."` adds a closing title with exactly that text at the end;
  `editorial.scene_titles: true` lets the **LLM planner** show the outline's scene titles (it may only copy the outline's title of the scene; any other text, such as a place or date, is rejected). `editorial.title_seconds`
  (default 3) is the on-screen time of the opening and closing titles, `editorial.max_titles` (default 6) the cap. The baseline planner supports the opening and closing titles too.
- **Rules**: one line, at most 60 characters (FilmCraft names a text clip after its first line, so a two-line title would fail the read-back), 1 to 10 seconds, inside the sequence, no two titles overlapping,
  none overlapping an overlay clip on V2. A title whose text equals a folder name of its clips is accepted but flagged "from folder name" in the plan's uncertainties. In `edit_plan.json` you may type your own title text (`text_source: "user"`); it is
  checked for structure, not origin; run `trip-vlog reseal` after editing.
- **Position/size**: lower-left, sized from the sequence height (the adapter's defaults); change them in FilmCraft.
- **Resolve**: no interchange format carries titles, so `handoff_report.md` lists them. `trip-vlog resolve-finish` recreates them from the same timeline spec as editable Text+ titles on V2 at the same frames
  (experimental, Studio only), verified by read-back, and lists any it cannot place. Run `resolve-finish --dry-run` first to see what it would do.
- **Older plans** (schema 1.0) still load; they simply have no titles.

## Captions (optional)

Captions are **off by default** (DESIGN §13.4). With `captions.enabled: true` (or the older `project.include_subtitles: true`) the handoff step writes `resolve_handoff/timeline.srt` next to `timeline.xml`,
plus `captions_report.json`, and `handoff_report.md` and the Resolve import notes describe it. Run `trip-vlog transcribe` first (a speech backend, see below): captions come from transcripts only.

```yaml
captions:
  enabled: true
  max_chars_per_line: 42      # lines are broken between words; a word longer than this stays whole
  max_lines: 2                # more text becomes more cues
  min_duration_seconds: 1.0   # a shorter cue stays up longer when its clip has room
```

- **Only the transcript's words.** Nothing is paraphrased, corrected, translated or completed. The options only decide where a segment is split into cues (at word boundaries, by line length and number of lines) and how long a cue stays up.
  Text without spaces (for example Japanese) is not broken. Words are never invented from filenames, metadata or the model.
- **Time is mapped through the edit plan**: record time = clip start + (source time - clip `source_in`), and a cue is clipped to the clip's used source range. A segment a clip cuts through keeps only the words whose estimated position lies inside the clip.
  The transcript has segment timing only, so inside a segment the position of a word, and therefore where a long segment is split, is **estimated from its place in the text**; check the captions against the speech.
- **Skipped, with a reason in `captions_report.json`**: stills, clips that are muted or `ambient_low` (the speech is not played at full level), tracks that carry no speech (music), **overlay clips** (reason "overlay audio is not in the timeline": overlays are placed without their audio, so nothing is heard; only the primary track and standalone natural-audio clips are captioned), clips not at normal speed (the plan has no speed changes, so this guards against a plan edited to have one), assets without a transcript of their current content, and
  transcript segments with `timestamps: coarse` (one block for the whole file, no real timing). Text is never dropped: cues that overlap in time are all kept, in start order (SRT allows overlap; the report counts them as `cues_overlapping`).
- **File format**: UTF-8 **without** a byte order mark, CRLF line endings (the SRT convention), `HH:MM:SS,mmm` timestamps. If you polish `timeline.srt` by hand, the next handoff keeps your version as `timeline.<hash>.bak.srt` before writing a new one; with captions turned off, or no usable text, an earlier file **that this tool wrote** is set aside as `timeline.<hash>.stale.srt` (named by its content, so an older set-aside file is never overwritten; a `timeline.srt` the tool never wrote is left alone; a file locked by another program is left in place with a warning). Nothing is deleted.
- **Resolve**: importing the SRT is a **manual step** (File > Import > Subtitle..., wording varies by version); trip-vlog does not import it and `resolve-finish` does not touch it. Whether it lines up in Resolve (including against the timeline's start timecode) is **not verified live** yet.
- A remote speech endpoint is not involved here: captions are made from transcripts already in the catalog, with no network access. Cache key of the handoff stage includes the caption options and the current transcripts.

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
  chunk_seconds: 600             # HTTP backend only: longer audio is uploaded in chunks of this length (25 MB limits, retries); with wav and a remote endpoint at most 700
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

`chunk_seconds` bounds each upload: 16 kHz mono 16-bit WAV is about 1.9 MB per minute, so against a remote (non-loopback) endpoint with `audio_format: wav` a value above 700 s
(hosted services reject uploads over 25 MB) is a config error, as for any non-loopback server including LAN ones; use `flac` for longer chunks, or a local server, which has no such limit.

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

- **Not verified live:** the QC critic (Phase 5b) and natural-language revisions (Phase 6) have only been exercised with fake endpoints and the generated fixtures (see the revision runbook items in DESIGN §20 Phase 6). `--planner llm` has only been exercised with fake endpoints and the generated fixtures; try it on your own footage before making it the default.
  The baseline planner is chronological with fixed clip lengths, stills at a default duration, no music or narration, hard cuts, and titles only when configured.
- Titles are one line only; their size and position are fixed defaults; `resolve-finish` titles are still experimental.
- Resolve import is unverified by this tool (see above). Titles are not carried by any interchange format; audio fades appear as dissolves in OTIO;
  FCPXML drops non-V1 transitions; EDL carries one video track. Only the FilmCraft 0.13.0 / Linux combination has been run; Windows and macOS are unverified until CI has run.
- Failed or interrupted assemblies leave their numbered `.fcproj` in `filmcraft/` (marked failed, never used, listed by `status`); it is not deleted automatically.
- Captions (`captions.enabled`) have not been imported in Resolve yet; word positions inside a transcript segment are estimates (segment timing only).
- Black-frame detection runs on the preview only and is reported as a warning; subjective judgments (does a cut feel abrupt, is the story clear) need the optional QC critic (`qc.critic`).
- HEIC/HEIF photos need ffprobe support for those formats (Pillow has no HEIC decoder here); without it they are listed as failed, and no thumbnail is made.
- Fast fingerprints include mtime, so a plain copy that does not preserve mtime is not detected as a duplicate (use `--fingerprint sha256`).
  Switching fingerprint mode refreshes `content_version` without marking assets stale, so a real change made at the same moment goes unnoticed.
- A truncated JPEG/PNG that Pillow can still open passes the probe and fails later at the thumbnail step (reported as a thumbnail error).
- HDR clips get ordinary (not tone-mapped) thumbnails; they look flat.
- Failed probes and failed thumbnails are retried on every run; they are not cached.
- Probing is sequential and single-threaded.
- Not yet verified on real phone footage (rotation, QuickTime location, VFR) beyond canned ffprobe JSON samples in `tests/fixtures/ffprobe/`.

### Handoff compatibility report

`resolve_handoff/compatibility_report.json` / `.md` (written by the `handoff` stage; `status` and `handoff_report.md` point to it) has one row per feature the plan
actually uses (frame rate/size, hard cuts, dissolves, stills, overlays on V2, titles, markers, clip gain, muted clips, music track, mixed source frame rate, a different
aspect ratio/orientation (plain scaling, e.g. 4K into 1080p, is not flagged), speed changes if any XML item has them): the count in the plan; whether FilmCraft supports it (capability snapshot of the workspace and the
read-back after reopening); whether the exported `timeline.xml` really contains it (the XML is parsed and every clip's record and source in/out, track, path,
gain, title, transition and marker is compared with the timeline sent to FilmCraft **at frame precision, tolerance 0**, any mismatch is listed; a dissolve the
engine could only place as a hard cut counts as asked-but-missing); and the **recorded** Resolve
result. Verdict per feature: `verified` (FilmCraft + XML ok and Resolve recorded/measured to import it), `expected` (Resolve unverified or partly recorded: cuts and dissolves are only partly recorded, so they are `expected`, not `verified`),
`manual` (a step in Resolve is needed; the instruction is given; a dissolve the engine downgraded to a hard cut is always `manual`: add it in Resolve), `unknown` (no FCP7 XML was exported,
so nothing could be checked; the overall result is then `unknown`, not `problem`) or `unsupported` (FilmCraft cannot, or the XML does not carry it as planned; the handoff stage then
ends `partial`). The recorded results are a static, versioned table (`trip_vlog/handoff/resolve_evidence.py`) holding only what DESIGN §15.5/§15.6 record on
Resolve Studio 21.1.1.10 (Windows); everything else is `unverified`. If a real `resolve_finish_report.json` of this very `timeline.xml` exists (it must record the XML's sha256; otherwise it is ignored), its per-check
results replace the table for the features those checks cover; run `trip-vlog resume` after `resolve-finish` to refresh the report.
