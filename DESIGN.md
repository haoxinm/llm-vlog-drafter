# DESIGN.md --- Agentic Travel-Vlog Drafting with FilmCraft

**Status:** Implementation specification\
**Audience:** A fresh coding agent implementing the system\
**Primary outcome:** Turn a folder of trip photos and video clips into a
coherent, editable first-draft vlog using FilmCraft, then hand the
project to a human for finishing in DaVinci Resolve.

**Revision 1.1 (approved changes):** early deterministic end-to-end slice
(§20); LLM selects moments while code computes exact source ranges (§8.3,
§10); deterministic candidate-moment segmentation with vision labeling
(§7.2); configurable vision sampling defaulting to 1 fps (§4.1, §7.2);
path-based asset IDs with content-fingerprint versions (§6.1); SQLite as
the single catalog store (§5.1, §6); default LLM provider plus
record/replay provider (§22, §19); explicit approval stop after the
outline (§9.5); handoff fallback from FilmCraft read-back state and a
user-run Resolve verification script (§15); Windows and macOS as
first-class platforms (§22.1); Resolve 21.1 observed results, FCP7 XML
as primary handoff, and an optional Studio-only `resolve-finish` step
via Resolve's scripting API (§15.5, §15.6).

------------------------------------------------------------------------

## 1. Executive summary

Build a local-first, resumable pipeline that:

1.  Indexes original photos and video files without modifying them.
2.  Extracts technical metadata, representative frames, scene
    boundaries, and optional speech transcripts.
3.  Produces a searchable media catalog with stable IDs and
    evidence-backed descriptions.
4.  Uses an LLM to propose a narrative outline and then a structured
    edit plan.
5.  Validates the plan deterministically.
6.  Executes editing operations through FilmCraft's documented agent
    interfaces (prefer MCP/CLI; use the JSON control bridge when driving
    a running desktop session is necessary).
7.  Saves a native FilmCraft `.fcproj` draft, renders a review preview,
    and creates a DaVinci Resolve handoff package.
8.  Supports targeted natural-language revisions without rebuilding
    unrelated sections.

**FilmCraft is the editing engine and source of truth for the draft
timeline.** Do not implement a competing timeline model that is rendered
independently by FFmpeg. FFmpeg or other media utilities may be used for
media probing, thumbnails, proxy generation, audio extraction, and
diagnostics, but all draft edit decisions must be represented in
FilmCraft.

The system is an assistant, not an autonomous final editor. It creates a
good draft and leaves final aesthetic decisions, color work, audio
polish, and final delivery to the user in DaVinci Resolve.

## 2. Product goals and non-goals

### 2.1 Goals

-   Produce an editable travel-vlog draft from a mixed collection of
    videos and still images.
-   Preserve source traceability: every selected moment maps back to an
    original file and source time range.
-   Prefer story and memorable moments over exhaustive coverage.
-   Let the user specify target duration, aspect ratio, tone,
    chronological preference, required moments, exclusions, music
    policy, narration policy, and output location.
-   Use schema-constrained LLM outputs and deterministic validation.
-   Operate in resumable stages with cached artifacts; a small revision
    should not require reanalyzing all media.
-   Drive FilmCraft through its documented interfaces and discover
    current commands at runtime rather than relying on guessed command
    IDs or undocumented project mutations.
-   Create a handoff that can be manually polished in DaVinci Resolve,
    with clear reporting of interchange limitations.
-   Keep original media read-only and keep project artifacts auditable.

### 2.2 Non-goals for the first version

-   Fully automated final color grading or audio mastering.
-   Training a custom vision, speech, or editing model.
-   A general-purpose video editor UI.
-   Automatic generation of factual narration without evidence.
-   Guaranteed perfect transfer of every FilmCraft effect, title,
    keyframe, or transition into Resolve.
-   Modifying the FilmCraft application or maintaining a fork.
-   Directly editing FilmCraft's internal project JSON as the primary
    mutation API.
-   Automatic uploading of private trip footage to third-party services
    without explicit configuration and consent.

## 3. FilmCraft integration requirements

FilmCraft is an alpha-stage open-source NLE with an agent-oriented
command system. Its documented interfaces include a CLI, MCP server,
JSON-lines control channel, and UI automation. The same engine commands
are used across interfaces.

### 3.1 Required discovery step

Before implementing editing calls, the coding agent MUST inspect the
current repository documentation and installed binary version. Use these
sources as the canonical references:

-   Repository: https://github.com/storytold/filmcraft
-   Agent guide:
    https://github.com/storytold/filmcraft/blob/main/docs/agents.md
-   Control protocol:
    https://github.com/storytold/filmcraft/blob/main/docs/control-protocol.md
-   Project files:
    https://github.com/storytold/filmcraft/blob/main/docs/project-files.md
-   Architecture:
    https://github.com/storytold/filmcraft/blob/main/docs/architecture.md
-   README section "Built for agents"

Do not assume that a command, parameter, MCP tool name, import format,
or export format in this design is guaranteed to exist in every version.
Discover it from the installed FilmCraft CLI/MCP tool descriptions and
current docs. Record the version and relevant capabilities in the run
manifest.

### 3.2 Preferred control strategy

Use this order of preference:

1.  **FilmCraft MCP headless session** for reproducible project
    construction and automated tests, targeting an explicit `.fcproj`
    project or a fresh session as documented by the installed version.
2.  **FilmCraft CLI** for command discovery, media/project operations,
    import/export, and diagnostics that are exposed through the CLI.
3.  **FilmCraft desktop control bridge** when a visible application
    session is required for screenshot inspection, previewing, or
    human-in-the-loop operations.
4.  UI mouse/keyboard automation only when an operation is not available
    through a documented command interface.

Do not use screenshots and mouse clicks as the default editing mechanism
if an engine command exists.

### 3.3 Command discovery and safety

At startup:

-   Detect FilmCraft executable paths and version.
-   Query available CLI commands and MCP tools.
-   Build a capability map of the operations actually available:
    create/open/save project, import media, inspect project/session,
    create sequence, place clips, trim, move clips, add tracks, add
    markers, add titles/captions, configure audio, render/export, and
    export interchange.
-   Fail with an actionable message if a required capability is absent.
-   Keep a small adapter layer between application logic and
    FilmCraft-specific calls.
-   Validate command parameters locally before invoking FilmCraft.
-   After mutation, query project/session state or save and re-open the
    project to verify that the change took effect.
-   Never invent command IDs or silently substitute a different command
    with different semantics.

FilmCraft's desktop control channel is loopback-only in the documented
protocol and uses a session token. If the bridge is used, follow the
current protocol documentation, generate a fresh secret token per
session, keep it out of logs, and do not expose the control port to the
network.

### 3.4 Native project and interchange

-   The `.fcproj` project is the authoritative draft.
-   Prefer documented FilmCraft engine commands for all project
    mutations.
-   Do not directly mutate `.fcproj` JSON unless the current
    documentation explicitly supports the operation and the change is
    validated by reopening the project. The format has a schema version
    and can change.
-   Preserve the `.fcproj` file and a copy of the validated edit plan.
-   Test interchange to Resolve early using a tiny project containing a
    video clip, a still, two tracks, one transition, one title, and one
    audio clip.
-   Use a format currently documented and supported by both
    applications, such as OTIO or FCP7 XML where suitable. Do not assume
    AAF/OTIO/XML feature parity.
-   Treat the rendered preview as a review artifact, not the handoff
    source of truth.
-   If an interchange format loses unsupported effects, titles, timing
    details, or media links, report the limitations and leave the user a
    clean FilmCraft project plus a compatibility report.

## 4. User experience and project configuration

The initial interface can be a CLI. Do not build a web UI until the core
pipeline is reliable.

Example invocation (exact CLI syntax is implementation-defined):

``` bash
trip-vlog draft \
  --media /path/to/trip-media \
  --output /path/to/vlog-project \
  --duration 180 \
  --aspect 16:9 \
  --style personal-cinematic \
  --filmcraft-mode mcp
```

The program should support a config file and command-line overrides.

### 4.1 Configuration fields

`project.yaml` or equivalent:

``` yaml
project:
  title: "Trip Vlog"
  target_duration_seconds: 180
  aspect_ratio: "16:9"
  frame_rate: "30"
  language: "en"
  story_mode: "mostly_chronological"
  style: "personal, cinematic, authentic"
  audience: "friends and family"
  include_narration: false
  include_subtitles: false
  music_policy: "user_supplied_only"
  preserve_natural_audio: true
  target_loudness_lufs: null

editorial:
  must_include: []
  must_exclude: []
  preferred_topics: []
  max_similar_shots_per_scene: 2
  opening_hook_seconds: 8
  closing_seconds: 10
  allow_nonlinear_hook: true
  avoid_fabricated_context: true
  avoid_excessive_transitions: true

media:
  recursive: true
  follow_symlinks: false
  fingerprint: "fast"            # fast | sha256
  proxy_policy: "on_demand"
  vision_sampling_fps: 1.0       # frames per second sent to the vision model
  fine_sampling_fps: 4.0         # optional dense pass for boundary refinement
  fine_sampling_enabled: false
  segment_window_seconds: 6.0    # max length of a deterministic candidate segment
  transcribe_speech: true
  local_only: false              # every LLM endpoint used must be loopback (§22)

llm:
  mode: "live"                   # live | record | replay
  text: "main"                   # endpoint profile for text/planning calls
  vision: "main"                 # endpoint profile for vision calls
  endpoints:
    main:
      api: "anthropic_messages"  # anthropic_messages | openai_chat | openai_responses
      base_url: "https://api.anthropic.com"   # http://127.0.0.1:<port> = local LLM
      path: null                 # null = API default (/v1/messages, /v1/chat/completions, /v1/responses)
      model: null                # or env named by model_env; never hard-coded
      api_key_env: "ANTHROPIC_API_KEY"        # env var name; null = no auth
      timeout_seconds: 120
      structured_output: "native" # native | json_prompt | none
      params: {}                 # passed through verbatim (temperature, top_p, seed, ...)

workflow:
  approval: "stop_after_outline" # stop_after_outline | auto_approve

filmcraft:
  mode: "mcp"
  executable: null
  control_host: "127.0.0.1"
  control_port: null
  project_name: "Trip Vlog Draft"
  sequence_name: "Main Draft"
  prefer_native_project: true
  interchange_formats: ["fcp7_xml", "otio"]   # FCP7 XML primary (§15.5)

outputs:
  project_file: "filmcraft/TripVlog.fcproj"
  preview_file: "preview/draft.mp4"
  edit_plan_file: "plans/edit_plan.json"
  interchange_dir: "resolve_handoff"
```

Configuration is illustrative. Validate allowed values and provide
sensible defaults. Secrets and API keys must be supplied through
environment variables or a secrets manager, never committed to the
project.

### 4.2 Interactive questions

If no config is provided, ask only for essential choices in a concise
setup step:

-   Target length (default 3 minutes).
-   Landscape 16:9 or vertical 9:16.
-   Style (personal/cinematic, energetic, documentary, relaxed).
-   Include narration/subtitles?
-   Required moments and exclusions.
-   Local-only processing or cloud models permitted?
-   Preferred music source or no music.

The pipeline must still run with defaults if optional answers are
skipped.

## 5. Proposed architecture

``` text
                    ┌─────────────────────────┐
                    │ CLI / project config    │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ Orchestrator / run state│
                    └────────────┬────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Media inventory and technical probe        │
           │ hashes, metadata, timestamps, orientation  │
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Media intelligence                         │
           │ scenes, thumbnails, ASR, frame descriptions│
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Catalog / evidence store                   │
           │ assets, moments, transcript, provenance    │
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Editorial planner (LLM)                    │
           │ outline → selections → structured edit plan│
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Deterministic plan validator               │
           │ IDs, ranges, duration, duplicates, tracks  │
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ FilmCraft adapter                          │
           │ MCP / CLI / bridge command execution       │
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ Verification, preview, critic, revisions   │
           └─────────────────────┬──────────────────────┘
                                 │
           ┌─────────────────────▼──────────────────────┐
           │ .fcproj + preview + Resolve handoff        │
           └────────────────────────────────────────────┘
```

### 5.1 Components

1.  **CLI/config loader** --- validates config, paths, options, and
    privacy policy.
2.  **Orchestrator** --- manages stages, retries, caching, resumability,
    and logs.
3.  **Media probe** --- reads media metadata and hashes, without
    altering originals.
4.  **Media analyzer** --- creates thumbnails/contact sheets, detects
    scenes, extracts audio, transcribes speech, and requests visual
    descriptions.
5.  **Catalog store** --- SQLite (`media_index/catalog.sqlite`) is the
    single source of truth for assets, segments, moments, transcripts,
    and analysis cache entries, with stable IDs and source provenance.
    JSONL files (`assets.jsonl`, `moments.jsonl`, `transcripts.jsonl`)
    are human-readable exports regenerated from SQLite and are never
    read back as authoritative input.
6.  **LLM gateway** --- provider-neutral interface for structured
    outputs, retries, token budgeting, and schema validation.
7.  **Editorial planner** --- creates outline and edit plan from catalog
    evidence.
8.  **Plan validator** --- deterministic correctness checks before
    editing.
9.  **FilmCraft adapter** --- maps abstract edit operations to the
    currently available FilmCraft commands.
10. **Quality checker** --- checks project structure, timeline timing,
    preview, audio, and handoff.
11. **Resolve exporter** --- exports a supported interchange format and
    writes a transfer report.
12. **Human review loop** --- records user decisions and translates
    revisions into localized plan changes.

Avoid a multi-agent framework initially. Implement a sequential, typed
workflow with one editorial LLM role and an optional independent critic.
Separate modules and typed interfaces are more important than multiple
autonomous agents.

## 6. Data model and workspace layout

Recommended generated workspace:

``` text
vlog-project/
  project.yaml
  manifest.json
  README.md
  media_index/
    catalog.sqlite          # authoritative catalog + analysis cache
    assets.jsonl            # export (read-only view)
    moments.jsonl           # export (read-only view)
    transcripts.jsonl       # export (read-only view)
    contact_sheets/
    thumbnails/
    proxies/
  plans/
    story_outline.json
    edit_plan.json
    edit_plan.schema.json
    revisions/
  filmcraft/
    TripVlog.fcproj
    capability_snapshot.json
    command_log.jsonl
  preview/
    draft.mp4
    contact_sheet_timeline.jpg
    qc_report.json
  resolve_handoff/
    timeline.otio
    timeline.xml
    media_manifest.csv
    handoff_report.md
  logs/
    run.jsonl
    errors.jsonl
```

Only produce interchange formats that are actually supported by the
installed FilmCraft version. The layout may omit unused artifacts. Do
not copy all original media into this directory by default; reference
source files and optionally generate proxies.

### 6.1 Stable IDs

-   `asset_id`: stable ID derived from the normalized relative path
    (POSIX separators, Unicode NFC) under the media root, e.g.
    `asset_` + first 16 hex chars of SHA-256 of that path. Do not use
    bare filenames as IDs. The ID survives an in-place content change so
    existing plans keep resolving.
-   `content_version`: a content fingerprint stored alongside the asset.
    Default `fast` = SHA-256 over (file size, mtime, first 4 MiB, last
    4 MiB); `sha256` = full-file hash. A changed fingerprint marks the
    asset's analysis stale and flags plans that reference it for
    revalidation; it does not change `asset_id`.
-   A file whose fingerprint matches a previously indexed asset at a
    different path is reported as a probable move/duplicate; it is not
    silently merged.
-   `moment_id`: stable ID for a candidate interval within an asset.
-   `scene_id`: stable ID for an editorial scene.
-   `clip_instance_id`: unique ID for one use of an asset/moment in the
    timeline. The same source can appear more than once intentionally.
-   Keep source path, source hash, file size, modification time, and
    metadata in the manifest.
-   Detect a changed file by its `content_version`; invalidate only
    dependent cached analysis.
-   Never infer a missing source from a similar filename.

### 6.2 Asset record

``` json
{
  "asset_id": "asset_...",
  "relative_path": "day-02/IMG_1042.MOV",
  "media_type": "video",
  "content_version": "fast:...",
  "sha256": null,
  "duration_seconds": 42.4,
  "width": 3840,
  "height": 2160,
  "frame_rate_num": 30000,
  "frame_rate_den": 1001,
  "rotation_degrees": 0,
  "capture_time": "2026-10-31T16:22:10-10:00",
  "gps": null,
  "audio_streams": 1,
  "technical_notes": [],
  "analysis_status": "complete"
}
```

All optional metadata fields can be null. Preserve the original
timestamp and timezone information when available; do not silently
convert an unknown local time into UTC.

### 6.3 Candidate moment record

``` json
{
  "moment_id": "moment_...",
  "asset_id": "asset_...",
  "source_in_seconds": 8.0,
  "source_out_seconds": 14.5,
  "description": "A wide view of waves breaking along a rocky coast",
  "visible_entities": ["ocean", "coastline"],
  "event": "waves breaking",
  "speech_summary": null,
  "transcript_refs": [],
  "quality": {
    "sharpness": null,
    "stability": null,
    "exposure": null
  },
  "editorial_tags": ["establishing", "nature"],
  "confidence": 0.83,
  "evidence": [
    {"type": "frame", "timestamp_seconds": 10.0, "artifact": "media_index/thumbnails/...jpg"}
  ]
}
```

Descriptions must be grounded in observed media. Mark uncertain labels
as uncertain. Do not claim that a location, person, activity, or spoken
phrase was identified unless there is evidence.

## 7. Media ingestion and analysis

### 7.1 Safe inventory

-   Recursively enumerate supported image and video extensions.
-   Skip hidden/system files and unsupported formats with a report.
-   Do not follow symlinks by default.
-   Never overwrite originals.
-   Compute the configured content fingerprint (fast by default, full
    SHA-256 when enabled).
-   Probe metadata with FFprobe or an equivalent reliable tool.
-   Handle corrupt, variable-frame-rate, rotated, HDR, high-frame-rate,
    and phone-recorded clips gracefully.
-   Store a per-file status and error rather than aborting the whole
    project for one bad file.

### 7.2 Frame and scene analysis

Candidate moments are found deterministically; the vision model labels
and scores them rather than discovering intervals itself:

1.  Extract a low-resolution thumbnail near the midpoint of each asset.
2.  **Deterministic segmentation:** split each video into candidate
    segments using scene-cut detection (e.g. FFmpeg scene score or
    PySceneDetect), audio-energy changes (onsets, silence boundaries),
    and a maximum window length (`segment_window_seconds`). Segments
    shorter than a minimum usable length are merged with a neighbour.
    Each segment gets a stable `segment_id` derived from `asset_id` and
    its frame-rounded bounds. Do not assume every scene cut is a new
    editorial moment.
3.  Sample frames at `vision_sampling_fps` (default 1 fps,
    configurable) within each segment, and create contact sheets with
    asset ID, segment ID, timestamp, and thumbnail.
4.  **Vision labeling:** the vision model receives segments (contact
    sheets or individual frames) and returns, per segment, a grounded
    description, tags, quality/interest scores, and optionally a
    sub-range *expressed as sampled-frame indices* it considers the best
    part. Code converts those indices to seconds. Labeled segments become
    candidate moments (`moment_id`).
5.  **Optional refinement:** when `fine_sampling_enabled`, sample more
    densely (`fine_sampling_fps`) around boundaries of high-scoring
    moments to tighten start/end. Off by default for cost.
6.  Preserve the sampled timestamp and evidence artifact for each
    description.

Use vision models in batches that fit context and payload limits. Prefer
contact sheets for triage but use individual frames for uncertain or
important moments. The model should not be asked to infer details that
are not visible.

### 7.3 Speech and audio

-   Extract audio to a temporary cache only when needed.
-   Make speech transcription optional and configurable.
-   Store transcript segments with start/end timestamps and confidence.
-   Distinguish spoken words from inferred summaries.
-   Retain useful natural sound (waves, birds, crowd ambience,
    reactions) as an editorial feature.
-   Detect whether source audio is silent, clipped, distorted, or
    dominated by wind.
-   Do not generate voiceover in v1 unless explicitly requested.
-   Do not add copyrighted music from an unlicensed source. Default to
    user-provided music or no music.

### 7.4 Photos

-   Keep original aspect ratio and orientation metadata.
-   Estimate duplicate/near-duplicate photos to avoid repetitive
    sequences.
-   Consider composition, sharpness, facial visibility, and variety, but
    do not automatically discard meaningful imperfect photos.
-   Represent a photo as a still-image clip in FilmCraft, with duration
    and optional restrained pan/zoom only if supported and tested.
-   If the still-image motion feature is unavailable or unreliable, use
    a static image rather than create an invalid edit.

### 7.5 Performance and caching

-   Cache by content hash + analyzer/model version + relevant
    configuration.
-   Store intermediate artifacts in predictable project paths.
-   Do not re-run transcription or vision analysis when only the target
    duration or story order changes.
-   Support proxy creation for high-resolution footage.
-   Bound concurrency and memory usage.
-   Make analysis interruptible and resumable.

## 8. Editorial planning

The LLM should operate in three phases rather than one monolithic
prompt.

### 8.1 Phase A: story outline

Input: - User project settings. - Compact catalog summary. - Candidate
moments with IDs, descriptions, timestamps, transcript summaries, and
quality indicators. - Must-include and must-exclude constraints.

Output: - A short narrative premise. - Ordered scenes with purpose,
emotional tone, approximate duration, and candidate moment IDs. - A list
of required-but-missing coverage. - Uncertainties that need user
confirmation. - No timeline commands at this phase.

Example scene roles: - opening hook - arrival/context - establishing
place - activity/action - personal reaction - local culture/food -
scenic breathing room - transition between places - reflection/ending

Use chronology by default for a travel memory vlog. A short nonlinear
hook is allowed if it is a real, compelling moment and does not
misrepresent what happened.

### 8.2 Phase B: selection and pacing

For each scene: - Choose a limited number of distinct moments. - Prefer
the strongest shot in a burst or cluster. - Balance wide, medium, and
close shots when available. - Use people and genuine reactions where
they add personal context. - Avoid repetitive views of the same beach,
meal, skyline, or activity. - Preserve a meaningful chronological arc
unless the user requests otherwise. - Allocate duration to importance,
not to how many files exist for a location. - Keep some natural sound as
punctuation rather than covering everything with music.

Use a combination of narrative value, uniqueness, visual quality,
emotional value, audio value, and technical usability. These are ranking
aids, not absolute rules. An imperfect clip can be the emotional
centerpiece.

### 8.3 Phase C: exact edit plan

The LLM produces a **selection plan**; code resolves it into an exact
**edit plan**. The LLM never writes source timestamps.

Selection plan (LLM output) per clip instance: `moment_id`,
`target_duration_seconds`, `anchor` (`start` | `center` | `end` |
`best` — where within the moment to take the cut), track role, audio
policy, transition intent, and rationale.

The **plan resolver** (deterministic code) then computes for each clip
the exact `source_in_seconds`/`source_out_seconds` inside the moment
bounds (clamping to the moment and asset duration, using the moment's
`best` sub-range when `anchor=best`), rounds to the sequence frame rate
with rational arithmetic, computes timeline positions from frame counts,
and records every clamp or shortfall as a resolver note. If a moment is
shorter than the requested duration, the resolver uses the whole moment
and reports the shortfall rather than extending beyond the moment
without consent.

The resolved edit plan contains: - Sequence settings. - Scene
order and target durations. - Clip instances with source asset ID,
source in/out, target track, and intended order. - Still-image duration
and optional motion. - Audio source ranges and mixing intent. -
Titles/captions only when supported by evidence and project
preferences. - Transition intent and duration. - A concise editorial
rationale. - A confidence score or uncertainty notes for nontrivial
decisions.

Do not let the LLM invent FilmCraft command names or parameters. It
outputs an application-independent plan; the adapter translates the plan
into discovered FilmCraft commands.

## 9. Prompt templates

Prompts should be stored as versioned files in the repository, not
buried in orchestration code.

### 9.1 Media analyst system prompt

> You are a careful media cataloger. Describe only what is supported by
> the supplied frames, clip metadata, and transcript. Separate direct
> observations from uncertain interpretations. Never invent a location,
> identity, activity, dialogue, or emotional state. Return valid
> structured data matching the supplied schema. Every proposed moment
> must reference an existing asset ID and source timestamps. If the
> evidence is insufficient, return an uncertainty note.

### 9.2 Story editor system prompt

> You are an experienced personal travel-vlog editor. Build a coherent
> and authentic story from the supplied media catalog. Optimize for
> memorable moments, genuine reactions, visual variety, and narrative
> progression rather than exhaustive coverage. Avoid repetitive shots
> and generic tourism clichés. Preserve chronology unless a brief
> opening hook improves the story without misrepresenting events.
> Respect all must-include and exclusion rules. Never invent events or
> facts. Every proposed shot must reference an existing candidate
> moment. First return a scene outline with purpose and duration budget;
> do not issue editor commands.

### 9.3 Timeline planner system prompt

> Convert the approved outline into an application-independent
> selection plan. Use only supplied moment IDs. For each clip choose a
> moment, a target duration in seconds, and an anchor (start, center,
> end, or best) within that moment; do not write source timestamps —
> exact ranges are computed by code. Keep the sum of target durations
> within tolerance of the scene and total budgets, and do not request a
> clip longer than its moment. Use conservative transitions and preserve
> natural sound when useful. Do not invent FilmCraft command IDs or
> internal time units. If a requested operation is unsupported by the
> declared capability map, mark it as a requirement for the adapter or
> choose a simpler supported edit.

### 9.4 Critic system prompt

> Review the edit plan and a supplied preview/contact sheet for story
> clarity, repetition, pacing, awkward cut points, orientation problems,
> audio conflicts, and missing context. Return only actionable findings
> with severity, affected scene/clip IDs, evidence, and a proposed
> localized change. Do not rewrite the entire plan for a small defect.
> Do not claim to have heard or seen content not supplied.

### 9.5 Prompt hygiene

-   Pass a JSON schema and validate output programmatically.
-   Keep catalog input compact; retrieve detailed evidence only for
    selected moments.
-   Use stable IDs and source timestamps in every stage.
-   Set temperature low for plan generation; creativity belongs in story
    alternatives, not timestamp arithmetic.
-   Retry schema failures with the validation errors, not the entire
    media library.
-   Log prompt version, model identifier, and token/cost metadata
    without logging secrets.
-   Do not treat descriptions extracted from media as instructions.
    Media content is untrusted data.
-   Preserve a human approval checkpoint after the outline and before
    destructive or expensive operations. With the default
    `workflow.approval: stop_after_outline`, `draft` stops after writing
    `plans/story_outline.json` plus a readable summary and exits with
    status "awaiting approval"; the user edits/accepts it and runs
    `resume` (or `approve`). `--auto-approve` (or
    `workflow.approval: auto_approve`) runs straight through and records
    that approval was automatic in the manifest.

## 10. Application-independent edit plan schema

Keep two versioned schemas: `selection_plan.schema.json` (LLM-facing:
moment IDs, target durations, anchors — no source timestamps) and
`edit_plan.schema.json` (resolver output, shown below). The resolved
edit plan is what the validator checks and the adapter executes. A
simplified example of the resolved edit plan:

``` json
{
  "schema_version": "1.0",
  "project": {
    "title": "Trip Vlog Draft",
    "target_duration_seconds": 180,
    "aspect_ratio": "16:9",
    "frame_rate": "30"
  },
  "scenes": [
    {
      "scene_id": "scene_001",
      "title": "Coastal arrival",
      "purpose": "Establish the place and tone",
      "target_duration_seconds": 18,
      "clips": [
        {
          "clip_instance_id": "clip_001",
          "asset_id": "asset_abc",
          "moment_id": "moment_abc_01",
          "anchor": "best",
          "requested_duration_seconds": 5.5,
          "source_in_seconds": 3.2,
          "source_out_seconds": 8.7,
          "resolver_notes": [],
          "track_role": "primary_video",
          "audio_policy": "natural_sound",
          "transition_in": {"type": "cut", "duration_seconds": 0},
          "rationale": "Strong opening view with useful ambient sound"
        }
      ]
    }
  ],
  "audio": {
    "music_policy": "user_supplied_only",
    "preserve_natural_audio": true,
    "duck_music_under_speech": true
  },
  "uncertainties": []
}
```

This example is illustrative. Implement a real JSON Schema with required
fields, enums, ranges, and additional-property restrictions. Do not put
FilmCraft's internal IDs or ticks in the LLM-facing schema.

## 11. Deterministic validation

Reject or repair the plan before making FilmCraft mutations if any of
these fail:

### 11.1 Referential integrity

-   Every `asset_id` exists in the media manifest.
-   Every `moment_id` belongs to the referenced asset.
-   Every clip instance ID is unique.
-   Every required moment is included or explicitly reported as
    unavailable.
-   No excluded moment is included.

### 11.2 Time correctness

-   `source_in_seconds >= 0`.
-   `source_out_seconds > source_in_seconds`.
-   `source_out_seconds <= asset.duration_seconds` within a small
    configurable tolerance.
-   All durations are finite and positive.
-   Sequence duration is within a configurable tolerance of the target.
-   Rounding to the sequence frame rate is done by code, never by the
    LLM.
-   Every resolved range lies inside its referenced moment's bounds
    (selection plans are validated for moment existence and duration
    feasibility before resolution; resolved plans are validated for all
    time checks above).
-   Use FilmCraft's documented time representation through the adapter;
    do not assume its internal tick values are seconds or nanoseconds.

### 11.3 Editorial checks

-   Duplicate or near-duplicate moments are flagged.
-   Repeated source ranges are allowed only when intentional and
    explained.
-   No scene exceeds its budget without a warning.
-   All still images have a positive display duration.
-   Transitions do not exceed the available adjacent clip durations.
-   Audio tracks and source ranges are valid.
-   Timeline overlaps are intentional and compatible with track design.
-   Output aspect ratio and source orientation are reviewed.

### 11.4 Repair policy

Attempt deterministic repairs only when the fix is unambiguous, such as
clipping an end time to a known source boundary or rounding a duration
to a valid frame. Record every repair. For ambiguous narrative
conflicts, return an error for the planner or user instead of silently
changing intent.

## 12. FilmCraft execution adapter

Create a narrow adapter API, for example:

``` python
class FilmCraftAdapter:
    def discover_capabilities(self) -> "CapabilityMap": ...
    def create_or_open_project(self, project_path: str) -> None: ...
    def import_media(self, media_paths: list[str]) -> "ImportResult": ...
    def create_sequence(self, settings: "SequenceSettings") -> "SequenceRef": ...
    def apply_edit_plan(self, plan: "EditPlan") -> "ExecutionReport": ...
    def inspect_project(self) -> "ProjectSnapshot": ...
    def save_project(self) -> None: ...
    def render_preview(self, output_path: str) -> "RenderReport": ...
    def export_interchange(self, output_dir: str) -> "HandoffReport": ...
```

Names above are internal interface suggestions, not asserted FilmCraft
commands.

### 12.1 Adapter implementation rules

-   Discover current FilmCraft commands/tool schemas from the running
    version.
-   Map internal operations to supported command IDs and typed
    parameters.
-   Normalize command results into typed internal responses.
-   Preserve raw request/response records in a sanitized JSONL command
    log.
-   Use a stable mapping from `asset_id` to FilmCraft media item ID and
    from `clip_instance_id` to timeline item ID.
-   After import, verify that media is online and linked to the expected
    original path.
-   After placement, query the timeline and verify source in/out, track,
    order, and duration.
-   Batch commands only if the interface documents safe
    batching/transaction behavior.
-   Use save checkpoints after project creation, media import, initial
    assembly, and successful revision.
-   If a command fails, stop dependent operations and report the
    failure; do not continue with a partially understood timeline.
-   Prefer a fresh generated project over mutating a user's unrelated
    FilmCraft project.

### 12.2 Idempotency and recovery

The same plan should not duplicate clips when re-run. Persist an
execution journal with: - plan version/hash, - FilmCraft version, -
project path, - imported media mapping, - clip-instance mapping, -
completed operations, - last successful checkpoint, - error details.

Before applying a plan, determine whether the existing project is a
managed project for this pipeline. If so, either reconcile the managed
sequence to the new plan using supported commands or create a new
versioned project. Do not repeatedly append the entire plan to an
existing timeline.

For the first implementation, versioned rebuilds are safer than clever
in-place reconciliation. Targeted revision support can be added after
stable clip-instance IDs and project inspection are tested.

## 13. Draft assembly policy

### 13.1 Sequence

-   Create one main sequence with the configured aspect ratio and frame
    rate.
-   Use a simple track layout: primary video/stills on V1, optional
    overlays/titles on V2, natural/source audio on A1, optional music on
    A2, and narration on A3 if enabled.
-   Adapt track naming and numbering to actual FilmCraft capabilities.
-   Use hard cuts by default; use dissolves only when they improve a
    real transition.
-   Keep titles minimal and editable.
-   Add markers at scene boundaries and optionally for QC issues.
-   If FilmCraft does not support a desired operation, prefer a simpler
    supported construction over external pre-rendering that would
    undermine editability.

### 13.2 Timing and frame rates

-   Keep all planning timestamps in seconds as decimal numbers.
-   Convert to the editor's exact frame/tick time only inside the
    FilmCraft adapter.
-   Use rational frame-rate values internally where available, such as
    `30000/1001`, not a rounded float.
-   Verify that clip duration and source range after conversion remain
    within one frame of the intended value.
-   Avoid accumulating timeline positions by repeated floating-point
    addition; calculate from frame/tick counts or the validated sequence
    order.

### 13.3 Audio

-   Preserve source audio when it contains meaningful sound or speech.
-   Use brief fades where supported and tested.
-   Keep music optional; never assume a track is licensed for reuse.
-   Duck music under speech only if the needed automation/keyframing is
    supported by FilmCraft and tested.
-   If automatic mixing is unavailable, create a conservative mix or
    leave a clear marker for manual adjustment.
-   Do not normalize or destructively alter original audio files.

### 13.4 Captions and titles

-   Default to no generated narration.
-   Subtitles should come from a transcript, not an invented paraphrase
    of what a person said.
-   Keep titles editable and use simple typography.
-   If interchange does not preserve text effects, document that and
    retain a version of the project in FilmCraft.
-   Do not generate factual location/date captions from weak metadata
    without flagging uncertainty.

## 14. Preview and quality-control loop

After the first assembly:

1.  Save the FilmCraft project.
2.  Render a low-resolution or draft-quality preview using FilmCraft's
    supported export path.
3.  Probe the output file for duration, dimensions, frame rate, and
    audio streams.
4.  Extract a contact sheet from the rendered preview.
5.  Inspect scene boundaries and sample frames around cuts.
6.  Run deterministic checks and optionally ask the critic LLM to review
    the contact sheet, scene list, and audio diagnostics.
7.  Apply only localized, explainable revisions.
8.  Save a new project checkpoint and rerender the preview.

### 14.1 QC checklist

-   All media is online.
-   No black gaps or unintended blank frames.
-   No accidental duplicate scenes.
-   Scene order is understandable.
-   No cut truncates essential speech or action.
-   No unintended source-audio discontinuity.
-   Music does not overpower speech.
-   Still images remain on screen long enough to read.
-   Portrait clips are framed reasonably in landscape sequences.
-   No unsupported titles/effects break preview rendering.
-   Total duration is within tolerance.
-   Output is playable and project reopens successfully.

The LLM can flag subjective issues; deterministic tools must verify file
existence, duration, track/range validity, and render success.

## 15. DaVinci Resolve handoff

The user will manually polish the draft in Resolve. Handoff reliability
is a first-class requirement.

### 15.1 Handoff package

Include: - Original FilmCraft `.fcproj`. - One interchange timeline in
the best supported format. - `media_manifest.csv` mapping asset IDs to
absolute source paths, file hashes, and metadata. - `handoff_report.md`
with FilmCraft version, export format, Resolve import instructions,
known limitations, missing media, and unsupported effects. - Optional
draft MP4 for quick comparison. - The validated application-independent
`edit_plan.json`.

Do not include source media copies unless explicitly requested. Use
absolute media paths in the manifest and warn if the project will be
moved to another machine.

### 15.2 Compatibility test

Before depending on the handoff, create a tiny fixture project in
FilmCraft and test: - one landscape video with source audio, - one
portrait video, - one still image, - two video tracks, - one audio/music
track, - one transition, - one title or caption, - a short trim and a
clip moved later on the timeline.

Export through each candidate interchange format that FilmCraft
currently supports. Import into the user's Resolve version and
compare: - timeline duration and frame rate, - clip order and in/out
points, - audio synchronization, - media relinking, - transitions, -
titles/captions, - image sizing, - track structure.

Record actual results. Do not promise full fidelity unless the test
demonstrates it. Start with the simplest timeline features and use only
the features that survive the round trip.

### 15.3 Fallback behavior

If FilmCraft's native interchange export is missing, fails, or loses
essential information (clip order, in/out points, media links):

-   **Read-back interchange:** generate OTIO (and, through OTIO
    adapters, FCP7 XML) from the timeline *read back from FilmCraft*
    via its documented inspection interface after save/reopen — never
    directly from the edit plan. This keeps FilmCraft the source of
    truth.
-   **Cross-check:** diff the read-back timeline against the resolved
    edit plan (clip order, source ranges, tracks, durations) and include
    the diff in `handoff_report.md`. Any mismatch is reported, not
    papered over.
-   Label the output as "generated from FilmCraft read-back" in the
    report, and list FilmCraft features (effects, titles, keyframes)
    that the read-back exporter does not carry.

In all failure cases: - preserve the FilmCraft project, - provide the
preview MP4, - provide the edit plan and media manifest, - generate a
report listing unsupported or unverified elements, - do not silently
substitute a flattened video as though it were editable.

### 15.4 User-run Resolve verification

Resolve cannot run in CI or a headless sandbox. Ship a script
(`trip-vlog verify-resolve`, or a standalone
`tools/resolve_verify.py`) that the user runs on their own machine with
DaVinci Resolve open. Using Resolve's documented scripting API
(`DaVinciResolveScript`; availability depends on Resolve edition and
external-scripting settings — detect and report), it:

1.  Creates a scratch Resolve project.
2.  Imports the handoff timeline file(s).
3.  Dumps the imported timeline: frame rate, duration, tracks, clip
    order, in/out points, media paths, online/offline status.
4.  Writes `resolve_handoff/resolve_verification.json` and compares it
    to the expected timeline, producing a pass/fail table per check in
    §15.2.

The tool records Resolve version and edition. If the scripting API is
unavailable, it prints precise manual import steps and a checklist
instead. Handoff fidelity is claimed only for checks this script (or
the user, manually) actually passed.

The user can still use the preview as a reference and manually
reconstruct or polish the timeline in Resolve if necessary.

### 15.5 Observed Resolve results and format policy

Manual import of the Phase 0 fixture into **DaVinci Resolve Studio 21.1
on Windows** (user-reported): FCP7 XML imports cleanly; OTIO imports
but its media is unlinked (relative `target_url` written by FilmCraft
when media and export share a top-level directory); FCPXML crashed
Resolve; EDL froze Resolve; AAF could not be opened. Policy:

-   Default `interchange_formats` is `["fcp7_xml", "otio"]`, FCP7 XML
    primary. OTIO is post-processed to absolute media paths and stays
    secondary until a Windows import with absolute paths is verified.
-   FCPXML, EDL and AAF are never exported by default; if configured
    explicitly, the handoff report carries a prominent warning with the
    observed results.
-   Scripted verification on Studio 21.1.1.10 (Windows,
    `tools/resolve_verify.py`): FCP7 XML imports via
    `ImportTimelineFromFile` with correct fps, duration, tracks, clip
    order, record/source in-out, linked media, the marker, and both
    transitions (Resolve lists transitions as timeline items). Titles
    are absent. **Stills whose file name contains digits (e.g.
    `still_1080p.png`, and typical camera names like `IMG_1042.JPG`)
    are misread as image sequences and arrive offline**; `resolve-finish`
    must detect and repair this. Verifier experiments on 21.1.1.10:
    `MediaPoolItem.ReplaceClip(<expected absolute path>)` on the
    misread item brings it online with the correct name and record
    range; importing without `sourceClipsPath`, or pre-importing media
    with `ImportMedia` and then importing the XML with
    `importSourceClips=False`, does not fix it. Markers survive the
    FCP7 XML import in this test. `resolve-finish` on a real 60-still
    workspace (21.1.1.10, Windows): `ReplaceClip` with the original path
    **returns False for a still that has numbered siblings in its folder**
    (e.g. `261010_stars_-318.jpg` … `-377.jpg`; Resolve sees one frame of
    an on-disk image sequence) and succeeds for a still without them.
    Copying that one file into its own folder
    (`resolve_handoff/stills/<key>/<name>`, original untouched) and
    calling `ReplaceClip` on the copy brought all 59 refused stills
    online; every required read-back check passed. The Resolve project
    then references those copies, so the folder must travel with it.
    OTIO is rejected by `ImportTimelineFromFile` in every
    path form tried, though the UI import accepts it; OTIO therefore
    stays an unverified secondary export.
-   Third-party measurements (see `docs/research/resolve-mcp.md`) report
    that FCP7 XML import drops markers and that titles arrive as
    media-less clips; the handoff report lists both so they can be
    recreated, and §15.6 automates that.

### 15.6 Optional `resolve-finish` step (Studio only)

An optional command, run by the user on their own machine with Resolve
Studio open, that finishes the handoff inside Resolve through Resolve's
documented scripting API (`DaVinciResolveScript`, the same discovery as
§15.4). It is a deterministic, allowlisted adapter, like the FilmCraft
adapter:

1.  Create a uniquely named Resolve project (never modify or delete an
    existing user project) and set frame rate and resolution from the
    edit plan before import.
2.  Import the FCP7 XML handoff timeline with a unique timeline name;
    detect offline media and image-sequence misreads and relink with
    `ReplaceClip` against the media manifest's absolute paths, falling
    back to an isolated single-file copy of a still when Resolve refuses
    the original (§15.5). Status: proven on 21.1.1.10.
3.  Re-add scene markers from the edit plan / FilmCraft read-back.
4.  Recreate titles as editable Resolve titles (Text+/Fusion title)
    on the intended track and frame range, using only documented API
    calls; if a title cannot be placed reliably, leave it listed in the
    report rather than approximate it. Status: implemented but not yet
    exercised on real Resolve; the edit plan gains titles in Phase 4.
5.  Read the Resolve timeline back and compare with the FilmCraft
    read-back (order, record/source in-out ±1 frame, fps, duration,
    media online, markers, titles), writing
    `resolve_handoff/resolve_finish_report.json` and `.md`.

Rules: no MCP layer and no third-party Resolve server is required; the
built-in Resolve 21.1 MCP server is not used by the pipeline because it
exposes arbitrary script execution rather than typed operations, and
the pipeline never lets an LLM issue Resolve commands (§17). Users may
still use Resolve's MCP with an assistant for manual polish outside
the pipeline. Requires Studio with external scripting set to Local;
check the Python version supported by the installed Resolve's
scripting library and report a clear error on mismatch. Without
Studio the plain FCP7 XML handoff (§15.1) remains the supported path.

### 15.7 Compatibility report (Phase 5a)

The handoff stage writes `resolve_handoff/compatibility_report.json` and `.md`: one row per feature the plan uses, with the count in the plan, FilmCraft
support, presence in the exported FCP7 XML (the XML is parsed and compared with the timeline sent to FilmCraft at frame precision; the real-FilmCraft
integration suite records this round trip as a test result: zero mismatches, tolerance 0 frames, for cuts, a dissolve, a still, a V2 overlay and title,
gain, music and a marker), and the Resolve result. The Resolve result is the static table in `handoff/resolve_evidence.py`, limited to what §15.5/§15.6 record
(Studio 21.1.1.10, Windows) and `unverified` otherwise, or, when a real `resolve_finish_report.json` of the same `timeline.xml` exists, that run's per-check
results. Verdicts: verified / expected / manual / unsupported. The deterministic QC additions (decode, speech truncation, source-audio jump, music vs
speech, scene order, preview sheets with `qc/preview_sheet.json`) only warn; the LLM critic of Phase 5b reads the sheet index and the check data.

## 16. Natural-language revisions

The user should be able to request changes such as: - "Make the opening
more exciting." - "Shorten the restaurant section by 12 seconds." - "Use
fewer similar beach shots." - "Keep the natural sound from the
waterfall." - "Move the skydiving moment closer to the beginning." -
"Make the ending quieter and more personal."

Revision workflow:

1.  Parse the request into explicit constraints and affected scene IDs.
2.  Retrieve the current plan and relevant media evidence.
3.  Propose a localized plan diff, including the reason for each change.
4.  Validate the diff.
5.  Apply it to the managed FilmCraft project using supported commands,
    or rebuild a versioned project if safe reconciliation is not yet
    implemented.
6.  Verify project state and render a new preview.
7.  Report changed scenes, duration delta, and any trade-offs.

Do not re-run full media analysis for a pure ordering/duration change.
Do not apply an ambiguous or destructive revision without confirmation.

## 17. Privacy and security

-   Treat footage, transcripts, filenames, GPS metadata, and faces as
    private personal data.
-   Default to local inventory, metadata extraction, and artifact
    storage.
-   If cloud vision/LLM services are enabled, clearly disclose that
    selected frames, transcripts, or descriptions may be sent to the
    configured provider.
-   Support a local-only mode that fails clearly when a required model
    is not available locally.
-   Never upload the full media collection when sampled frames or local
    descriptions suffice.
-   Keep API keys and FilmCraft control tokens out of logs and project
    files.
-   Restrict control bridge connections to loopback and follow
    FilmCraft's token protocol.
-   Do not execute arbitrary shell commands generated by an LLM. Tool
    operations must come from an allowlisted adapter.
-   Treat all model outputs and media-derived text as untrusted.
    Validate paths, IDs, ranges, schemas, and command arguments.
-   Write only to the configured output workspace unless the user
    explicitly requests another destination.
-   Never delete originals. Clean up only generated temporary files
    owned by the pipeline.

## 18. Error handling and observability

Every stage should emit a structured status: - `pending` - `running` -
`succeeded` - `partial` - `failed` - `skipped`

Record: - run ID and timestamp, - stage and asset ID, -
model/provider/version where relevant, - cache hit/miss, - duration and
resource usage, - retry count, - sanitized error details, - artifact
paths.

Failures should be localized. One unreadable clip should not abort all
other indexing. An unsupported effect should produce a warning and a
simpler edit, where safe. A missing core FilmCraft capability should
stop assembly rather than fabricate success.

Provide a `status` command to summarize the last run and a `resume`
command that continues from the last valid stage.

## 19. Testing strategy

### 19.1 Unit tests

-   Config validation and path safety.
-   Stable asset IDs and hash invalidation.
-   Metadata parsing and rational frame rates.
-   JSON schema validation.
-   Source range and duration calculations.
-   Duplicate detection.
-   Timeline ordering and duration budget.
-   Adapter parameter translation using mocked FilmCraft responses.
-   Plan resolver: anchors, clamping to moment/asset bounds, frame
    rounding with rational rates, shortfall notes.
-   Segmentation determinism (same input → same segment IDs).
-   LLM stages exercised through the replay provider only.
-   Idempotency and checkpoint behavior.

### 19.2 Integration tests

Use a small, redistributable or user-generated fixture set: - short
video with audio, - short silent video, - portrait video, - still
image, - one corrupt/unsupported file.

Test: - inventory and analysis, - plan validation, - FilmCraft project
creation and media import, - clip placement and save/reopen, - preview
rendering, - interchange export, - media relinking in the target Resolve
installation when available.

Do not put personal travel footage into public CI.

### 19.3 End-to-end acceptance test

Given a fixture media folder and default configuration, the system
must: 1. Create a manifest with stable asset IDs. 2. Generate a story
outline and a schema-valid edit plan. 3. Reject deliberately invalid IDs
and out-of-range source times. 4. Create a FilmCraft project with a
sequence and placed clips. 5. Save and reopen the project. 6. Render a
playable preview. 7. Produce a QC report and Resolve handoff report. 8.
Resume without duplicating clips after an interrupted run.

### 19.4 Golden and regression checks

-   Keep a tiny deterministic fixture project.
-   Verify timeline durations and source ranges exactly where possible.
-   Compare rendered frame samples with tolerances rather than requiring
    byte-identical exports across hardware/codec implementations.
-   Keep a snapshot of the FilmCraft capability map for debugging, but
    regenerate it when the app is upgraded.

## 20. Implementation phases and deliverables

### Phase 0 --- FilmCraft spike (must come first)

Deliver: - installation/version detection, - command/MCP discovery, -
minimal project creation, - media import, - one video and one still
placed on a sequence, - project save/reopen, - preview render, -
interchange experiment and compatibility report.

**Exit criterion:** prove the current installed FilmCraft can execute
and persist the basic edit operations through its documented agent
interface. Do not build the whole pipeline before this works.

### Phase 1 --- Media inventory

Deliver: - CLI/config, - media scanner and metadata probe, - hashes and
stable IDs, - contact sheets and basic cached artifacts, - per-file
error reporting.

### Phase 2 --- Deterministic end-to-end slice

Get the riskiest integration working before any LLM work. Deliver: -
deterministic segmentation into candidate moments (§7.2 step 2), -
selection/edit plan schemas, plan resolver, and deterministic
validation, - a **baseline planner** with no LLM (chronological order,
fixed per-clip durations, stills at a default duration, budgeted to the
target length), - FilmCraft adapter with capability discovery,
idempotent project creation and clip placement, checkpoints and command
logs, basic audio and still-image support, - save/reopen verification
and preview render, - deterministic QC, media manifest, interchange
export (native; the read-back fallback of §15.3 moved to Phase 7), handoff report, and the
user-run Resolve verification script (§15.4), - `status`/`resume`.

**Exit criterion:** `trip-vlog draft --planner baseline` turns the
fixture folder into a reopened `.fcproj`, a playable preview, and a
handoff package, and a re-run does not duplicate clips.

### Phase 3 --- LLM media catalog

Deliver: - LLM gateway with a default provider and a record/replay
provider, - vision labeling of deterministic segments at
`vision_sampling_fps` (optional dense refinement), - optional
transcription, - candidate moments with evidence, - versioned prompt
templates and schemas, - cache invalidation.

### Phase 4 --- LLM editorial planning

Deliver: - story outline generation with the approval stop, - LLM
selection plans feeding the existing resolver/validator, - duration
budgeting, - human-readable plan summary, - editable titles in the edit
plan (mapped to FilmCraft text clips and recreated by `resolve-finish`),
- `--planner llm` as the default once tested.

### Phase 5 --- QC critic and handoff hardening

Deliver: - preview contact sheet and critic report, - localized
critic-driven fixes, - handoff compatibility results recorded from the
fixture round-trip, - explicit compatibility report.

### Phase 6 --- Revision loop

Deliver: - natural-language revision parser, - localized plan diffs, -
versioned project updates, - rerender and change summary.

### Phase 7 --- Deferred hardening and polish

Items deferred from earlier phases. None blocks a usable draft; each
ships with tests like any other phase. Deliver:

-   **Read-back interchange fallback (§15.3):** generate OTIO / FCP7 XML
    from the timeline read back from FilmCraft when its native export
    is missing or lossy, with the cross-check diff in the handoff
    report. Not needed so far: FilmCraft 0.13's native FCP7 XML
    passed every check in Resolve.
-   **Plan re-seal helper:** a command that re-validates a hand-edited
    `edit_plan.json` / outline and recomputes its seal (hash), so
    manual edits are accepted explicitly instead of failing the hash
    check.
-   **Failed-assembly cleanup:** when a FilmCraft build fails midway,
    mark the partial `_vNNN.fcproj` as failed in the journal and keep
    it out of `resume`/handoff, without deleting user data.
-   **Portrait and mixed-aspect framing:** scale-to-fit portrait clips
    and stills in a landscape sequence (and the reverse), if FilmCraft
    supports it and the result is verified.
-   **Audio ducking:** lower music under natural sound or speech
    (§13.3), verified in the read-back and the preview.
-   **Fine sampling refinement (§7.2 step 5):** when
    `fine_sampling_enabled`, resample at `fine_sampling_fps` around the
    best-frame boundaries of high-scoring moments and tighten them with
    a second, smaller request (own prompt, schema and cache). Phase 3
    ships without it; enabling it only adds a warning.
-   **Captions and subtitles from transcripts (§13.4):** captions only
    from transcript text, never paraphrased, exported in a form the
    handoff can carry (e.g. an SRT alongside the timeline) and verified
    in Resolve.

## 21. Suggested repository structure

``` text
trip-vlog-agent/
  pyproject.toml
  README.md
  DESIGN.md
  src/trip_vlog/
    __init__.py
    cli.py
    config.py
    orchestrator.py
    state.py
    logging_utils.py
    media/
      scan.py
      probe.py
      thumbnails.py
      scenes.py
      transcription.py
      vision.py
      catalog.py
    media/segment.py          # deterministic candidate segmentation
    llm/
      gateway.py
      providers/anthropic.py
      providers/replay.py
      schemas.py
      prompts.py
      planner.py
      critic.py
    planning/
      models.py
      baseline.py             # no-LLM planner (Phase 2)
      resolve_plan.py         # selection plan → exact edit plan
      validate.py
      duration.py
      revisions.py
    filmcraft/
      discovery.py
      adapter.py
      mappings.py
      execution.py
      inspect.py
    qc/
      checks.py
      preview.py
      report.py
    handoff/
      interchange.py
      readback_otio.py        # fallback interchange from FilmCraft read-back
      media_manifest.py
      report.py
  tools/
    resolve_verify.py         # user-run Resolve import verification
  prompts/
    media_analyst.md
    story_editor.md
    timeline_planner.md
    critic.md
  schemas/
    asset.schema.json
    moment.schema.json
    story_outline.schema.json
    selection_plan.schema.json
    edit_plan.schema.json
  tests/
    unit/
    integration/
    fixtures/
  examples/
    project.yaml
```

The exact language and layout can change if the implementing agent has a
strong reason, but the separation of responsibilities should remain.

## 22. Technology choices

Prefer boring, replaceable tools:

-   **Python 3.11+** for orchestration and typed models, unless the
    environment imposes another version.
-   **Pydantic or JSON Schema validation** for structured data.
-   **FFprobe/FFmpeg** for probing, thumbnails, proxy generation, audio
    extraction, and output diagnostics---not as a second timeline
    renderer.
-   **SQLite + JSON artifacts** for cache/catalog persistence.
-   **A provider-neutral LLM gateway** over plain HTTP (no vendor
    SDK), supporting schema-constrained output and vision input. It
    speaks three API schemas, so it works with Anthropic, OpenAI and
    any provider or local server compatible with either:
    `anthropic_messages`, `openai_chat` (Chat Completions) and
    `openai_responses`. Each named endpoint configures address, path,
    timeout, retries, auth header from an environment variable, extra
    headers, the model ID (config or env, never hard-coded), the
    structured-output mode (native JSON-schema parameter, or the schema
    in the prompt for servers without one) and pass-through request
    parameters such as `temperature`. Output is always validated
    client-side against the schema. A loopback `base_url`
    (`127.0.0.1`, `localhost`, `::1`) marks a local LLM; `local_only`
    requires every endpoint in use to be local. Plain `http://` to a
    remote host is refused unless explicitly allowed. It ships with:
    -   a **record/replay provider**: in `record` mode it stores each
        request fingerprint (prompt version + schema + inputs hash +
        model) and the validated response under `tests/fixtures/llm/`;
        in `replay` mode it returns stored responses and fails loudly
        on a cache miss. Tests and CI use replay only, so they are
        deterministic and need no API key.
-   **Speech recognition** as an optional adapter, selected according to
    privacy and quality requirements.
-   **FilmCraft CLI/MCP** as the primary editing interface.
-   **Pytest** for unit and integration tests.

### 22.1 Platform support

**Windows 10/11 (x64) and macOS 13+ (Apple Silicon and Intel) are
first-class, required targets.** Linux is supported for development and
CI. A feature is not done until it works on both Windows and macOS.

-   **Paths:** use `pathlib` everywhere; never build paths by string
    concatenation or assume `/`. Normalize relative paths for IDs to
    POSIX separators and Unicode NFC (macOS file names often arrive as
    NFD). Do not change case for IDs; detect case-only collisions on
    case-insensitive volumes (default on Windows and macOS) and report
    them. Support Windows long paths (`\\?\` prefix where required) and
    drive letters/UNC paths in manifests. Treat paths written into
    interchange files per the target's expected URL/path form (e.g.
    `file:///C:/...` vs `file:///Users/...`) and test both.
-   **Executables:** discover FilmCraft, FFmpeg/FFprobe, and optional
    ASR tools per platform: `PATH`, configured paths, and standard
    install locations (`.exe` on Windows; `/Applications/*.app/Contents/
    MacOS/` and Homebrew prefixes `/opt/homebrew`, `/usr/local` on
    macOS). Record resolved paths and versions in the capability
    snapshot.
-   **Processes:** launch subprocesses with argument lists, never via a
    shell; no POSIX-only constructs (`fork`, signals other than
    terminate, `/dev/null` literals). MCP stdio sessions must handle
    Windows pipe behavior and CRLF-safe framing; terminate child
    processes reliably on both platforms (process groups on macOS, job
    objects or `CREATE_NEW_PROCESS_GROUP` on Windows).
-   **Files:** write JSON/JSONL/CSV as UTF-8 with `\n` newlines
    explicitly; use atomic replace (`os.replace`) for checkpoints;
    tolerate Windows file locking (retry briefly, never delete originals);
    avoid characters invalid on Windows in generated file names
    (`<>:"/\|?*`, trailing dots/spaces, reserved names like `CON`).
-   **Time and metadata:** do not rely on file creation time semantics
    that differ by OS; prefer embedded capture metadata, then mtime.
-   **DaVinci Resolve:** the verification script (§15.4) locates the
    scripting API at the documented per-OS locations (Windows
    `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\
    Scripting`, macOS `/Library/Application Support/Blackmagic Design/
    DaVinci Resolve/Developer/Scripting`), honoring
    `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB` if set.
-   **Packaging:** pure-Python package installable with `pipx`/`uv` on
    both OSes; no compiled extensions required for the core. Document
    FFmpeg and FilmCraft installation per OS in the README.
-   **CI:** run unit tests and fixture integration tests (with
    FilmCraft where its builds are available) on `windows-latest`,
    `macos-latest`, and `ubuntu-latest`. Record per-OS FilmCraft
    capability differences in the capability snapshot and the
    compatibility report; never assume a capability verified on one OS
    exists on another.
-   Phase 0 results gathered on one OS must be re-verified on Windows
    and macOS (in CI or by the user) before being relied on there.

Do not lock the architecture to one LLM vendor or agent framework. The
workflow should work with any model that meets the vision,
structured-output, context, and privacy requirements.

## 23. Operational policy and acceptance criteria

The implementation is successful when:

-   A user can point the tool at a media folder and get an editable
    FilmCraft draft.
-   The draft has a coherent story and avoids obvious repeated shots.
-   Every timeline clip can be traced to its source asset and source
    range.
-   Invalid plans are rejected before FilmCraft is mutated.
-   A failed run can resume without duplicate timeline clips.
-   The FilmCraft project saves and reopens.
-   A playable preview and QC report are produced.
-   The Resolve handoff is tested and its limitations are honestly
    reported.
-   Original files remain unchanged.
-   The tool installs and passes its test suite on both Windows and
    macOS.
-   The user can request targeted changes in natural language.
-   Provider/model and editor versions are recorded for reproducibility.

Quality should be measured by human review, not by whether a video file
exists. Useful evaluation questions: 1. Did the editor select the
moments the user considers memorable? 2. Does the vlog feel personal and
coherent rather than generic? 3. How many manual corrections are needed
in Resolve? 4. Were source audio and meaningful reactions preserved? 5.
Did the handoff preserve enough editability to justify using FilmCraft?

## 24. Instructions to the implementing agent

1.  Read this document in full before coding.
2.  Inspect the current FilmCraft README and agent/control/project
    documentation. The application is evolving; verify all commands and
    export capabilities from the installed version.
3.  Implement **Phase 0 first** and report the exact environment,
    discovered capabilities, successful commands, and interchange
    results.
4.  Do not claim success for a feature that was not executed and
    verified.
5.  Do not invent FilmCraft APIs, MCP tool names, internal tick units,
    or interchange guarantees.
6.  Keep the first milestone small: create a FilmCraft project with a
    few fixture clips and produce a valid preview.
7.  Use deterministic validation for IDs, timestamps, duration
    arithmetic, paths, and output files.
8.  Keep the LLM responsible for semantic/editorial choices, not
    low-level arithmetic or raw command construction.
9.  Prefer small, typed modules, versioned schemas, checkpoints, and
    clear logs over a large autonomous-agent framework.
10. After each phase, run tests and update README usage instructions and
    known limitations.

**Final deliverable:** a working local-first tool that produces a
well-structured FilmCraft draft project and a tested Resolve handoff
package, with a human remaining in control of final polish.
