# Phase 0 -- FilmCraft compatibility spike

Status: **exit criterion met for the headless MCP interface** (create, import, place, trim, move, tracks, transition, title,
marker, save, reopen, render all executed and read back). **DaVinci Resolve import: pending -- requires the user's Resolve
installation** (Resolve is proprietary GUI software that needs a GPU and cannot run in this sandbox; no Resolve compatibility is
claimed anywhere in this document).

Everything marked *executed* below was run by `spike/phase0_filmcraft_spike.py` (59 checks, 58 pass, 1 expected warning, exit 0;
raw evidence in `docs/phase0/capability_snapshot.json`). Items marked *docs only* were read, not run.

## 1. Environment

| Item | Value |
|---|---|
| OS | Linux 6.18 x86_64 (glibc 2.39), no GPU, no display (`DISPLAY` unset), 4 cores |
| FilmCraft | `filmcraft-cli 0.13.0`, git `693c41be5a93241e7c502e24d09a946c8a06944e` (`v0.13.0-14-g693c41b`, main, 2026-10-25), repo https://github.com/storytold/filmcraft |
| Newest tag | `v0.13.0` (via `git ls-remote --tags`); main is 14 commits past it. Alpha software (README) |
| Prebuilt binaries | Docs say releases ship `filmcraft-<v>-linux-x86_64.{AppImage,deb,rpm,tar.gz}` (CLI at `/usr/bin/filmcraft-cli` in deb/rpm) -- *docs only*. The GitHub releases page returned 403 through the sandbox proxy and the API is blocked, so existence of assets was **not verified** |
| Build | `cargo build --release -p filmcraft-cli` (rustc 1.97.0, 6 m 10 s from cold, 4 cores). No system `-dev` libraries, GPU, or display needed for the headless CLI/MCP crate. GUI crate (`filmcraft`) was not built |
| Tools | ffmpeg 6.1.1, python 3.13, optional `opentimelineio 0.18.1` + `otio-fcp-adapter`, `otio-cmx3600-adapter`, `otio-fcpx-xml-adapter` (pip), `xmllint` |

Reproduce:
```sh
git clone --depth 1 https://github.com/storytold/filmcraft && cd filmcraft
cargo build --release -p filmcraft-cli                      # -> target/release/filmcraft-cli
cd ../llm-vlog-drafter
python3 spike/phase0_filmcraft_spike.py --filmcraft-cli /path/to/filmcraft-cli --filmcraft-src /path/to/filmcraft
# (generates fixtures via tests/fixtures/make_fixtures.py if missing; outputs in spike/out/, git-ignored)
```

## 2. Discovered interfaces

Sources read: README "Built for agents", `docs/agents.md`, `docs/control-protocol.md`, `docs/project-files.md`,
`docs/architecture.md`, `crates/interchange/README.md`, `docs/releasing.md`, `docs/gaps.md`, plus engine source for
`file.exportInterchange` (behaviour confirmed by running it).

* **MCP server, headless**: `filmcraft-cli mcp [--demo | --project p.fcproj] [--data-dir D]` -- newline-delimited JSON-RPC on stdio.
  *Executed*: `initialize` -> protocol `2025-06-18`, server `filmcraft 0.13.0`, capabilities `tools`, `resources`; `tools/list` -> 17 tools
  (`docs/phase0/mcp_tools.json`). Bare `mcp` = empty project. No folder-confinement flags: the server reads/writes anywhere the user can.
* **MCP tools**: `command_list`, `command_run {id, params}`, `command_batch {steps, stop_on_error}`, `doc_inspect`, `project_inspect`,
  `sequence_inspect`, `media_import {paths, bin?, image_sequence?}`, `render_frame` / `render_preview` (`seconds`, `max_side`; returns a PNG image),
  and bridge-only `ui_click/ui_drag/ui_elements/ui_inspect/ui_key/ui_screenshot/ui_type/ui_control`. Unknown tool arguments are a `-32602`
  error (strict); a failing command is an `isError` result. Executed: both behaviours observed.
* **CLI** (`filmcraft-cli`): `exec`, `run script.jsonl`, `commands [--json]`, `describe`, `inspect`, `import`, `export`, `render`, `probe`,
  `mcp`, `--project/--demo/--save/--save-as/--bridge`. *Executed*: `--version`, `help`, `commands --json` (**710 engine commands**),
  `--project p inspect sequence`, `export` (Section 5 uses the MCP path; CLI export was exercised manually, 13.7 s, same result).
* **Desktop control channel / bridge** (`filmcraft --control PORT`, JSON lines on loopback TCP, `FILMCRAFT_CONTROL_TOKEN`): *docs only, not
  tested* (needs the GUI app and a display). Not required for the pipeline.
* One command registry serves CLI, MCP, control channel and UI; the adapter should talk to `command_run`/`command_batch` plus the three inspect tools.

## 3. Capability table (DESIGN.md 3.3)

Status: **verified** = executed and read back; **failed** = ran but wrong; **n/a**; **not tested**.

| Capability | Status | Tool / command (exact) |
|---|---|---|
| Detect executable + version | verified | `filmcraft-cli --version`; MCP `serverInfo.version` |
| Query commands / MCP tools | verified | `commands --json`, MCP `command_list`, `tools/list` (counts agree: 710) |
| Create project | verified | `command_run file.newProject {name}` |
| Open project | verified | `mcp --project x.fcproj` (new process); `file.open {path}` exists (*not exercised in-session*) |
| Save project | verified | `file.saveAs {path}` (`file.save`, `file.saveCopy` exist) |
| Import media (video, still, portrait, wav, m4a) | verified | `media_import {paths:[abs...]}` -> item ids; `file.import` also exists |
| Verify media online + original path | verified | `command_run media.status` -> `status:"online"`, `path`, fingerprint; re-checked after reopen |
| Create sequence 16:9 30 fps | verified | `file.newSequence {name,width,height,fps,video,audio}` |
| Place video (+linked audio) / still / audio-only | verified | `timeline.place {item, track:"V1"\|"A2", seconds, duration?, audio?}`; a movie lands as linked V+A clips |
| Trim (ripple and regular) | verified | `timeline.trim {clip, edge:"in"\|"out", mode:"ripple"\|"regular", deltaFrames}`; linked audio follows |
| Move clip later | verified | `timeline.move {moves:[{clip,track,time}]}` |
| Add video / audio tracks | verified | `sequence.addTracks {video,audio}` (tracks named "Video N"/"Audio N"; addressable as `"V2"`/`"A2"`) |
| Transition | verified | `sequence.applyVideoTransition {clip, edge, effect:"cross_dissolve", frames}` (center-aligned on the cut) |
| Title / text | verified | `graphics.newText {text, seconds, time, size, position, track(0-based index)}` |
| Marker | verified | `markers.add {time, name, comment, color}` |
| Audio gain | verified | `clip.audioGain {clips, mode:"set", db}`; `gainDb` read in `sequence_inspect` |
| Audio fade | verified | `sequence.applyAudioTransition {clip, effect:"constant_power", edge:"out", frames}` |
| Undo | verified | `edit.undo` |
| Inspect after mutation | verified | `sequence_inspect` (ticks + frames, source in/out, track, order, transitions, markers) |
| Render preview MP4 | verified | `file.exportMedia {path, format:"h264", wait:true}` (blocks; progress token + cancel supported per docs) |
| Preview still frame | verified | MCP `render_frame {seconds, max_side}` -> PNG image content |
| Interchange export | verified (export + own structure) | `file.exportInterchange {format: edl\|xml\|fcpxml\|otio\|aaf\|omf, path}` |
| Music ducking / keyframed automation | not tested | `effects.setParam` with keyframes exists in docs/catalog; nothing run |
| Captions from transcript | not tested | `captions.*` / transcript commands exist in catalog; nothing run |
| Desktop bridge / UI automation | not tested | needs GUI |
| Resolve import | **pending -- requires user's Resolve installation** | see Section 8 |

## 4. Time representation, project file

* **Ticks**: 254 016 000 000 per second (docs; confirmed: 5 s clip = 1 270 080 000 000). One 30 fps frame = 8 467 200 000 ticks. Commands accept
  `seconds=`, `frame=`, `timecode=`, ticks, or `deltaFrames`. `sequence_inspect` reports ticks **and** `startFrame/endFrame/durationFrames`;
  `sourceIn/sourceOut` are ticks in media time. Clips snap to the sequence frame grid.
* **Frame rate** stored as exact rational `{num,den}`. Executed probe: `fps=24000/1001` -> `24000/1001`, `30000/1001` -> `30000/1001`,
  `60000/1001` -> `60000/1001`, `25` -> `25/1` (so pass the float quotient; NTSC rates round-trip exactly).
* **`.fcproj`**: UTF-8 JSON, compact; `format:"filmcraft.project"`, **`schema_version: 15`**, `generator:"FilmCraft 0.13.0"`, keys `project{name,settings,root,items,next_id}` and `view`.
  Media referenced by absolute path (confirmed). Saves are atomic. `project-files.md` still shows an example with schema 13 (docs lag the code);
  files from newer builds are refused by older ones. The pipeline must not hand-edit the JSON.
* Imported default still duration = 5 s (the spike overrides with `duration` at placement).

## 5. Executed timeline, persistence and render

Built (frames at 30 fps): V1 = landscape src 0.5-4.0 s at 0-105, still 105-195 with a 15-frame cross dissolve centered at 105;
V2 = title "Hello Title" 0-60, portrait clip (video only) 60-120 (placed at 1 s, moved to 2 s); A1 = linked landscape audio 0-105;
A2 = 6 s wav 0-180 at -6 dB with a 15-frame constant-power fade out; marker "Scene A" at 2.0 s. Total 195 frames.

**Persistence**: after `file.saveAs`, the server was killed and a **new** `mcp --project` process reopened the file; the normalized timeline
signature (track, clip order, start/end frame, source in/out ticks, gain, transitions, markers) was identical, and `media.status` showed all media online.

**Render** (`file.exportMedia`, 13.1 s wall for 195 frames @1080p on CPU, ~15 fps) ffprobe:
`h264 yuv420p 1920x1080 r_frame_rate 30/1 nb_frames 195 duration 6.500000; aac stereo 48000 Hz duration 6.506`. Frame count equals the
sequence's `durationFrames`; the title is visible in an extracted frame. `render_frame` returned a PNG. Note FilmCraft creates a
`FilmCraft Previews/` directory next to the saved project.

## 6. Interchange results (all executed; no Resolve involved)

Validation = stdlib parsers compare tracks/clip order/start/duration/source-in/media path/transition count/markers against the built
timeline; OTIO loaded with `opentimelineio` 0.18.1; FCP7 XML loaded with `otio-fcp-adapter`; EDL with `otio-cmx3600-adapter`; XML formats `xmllint`-clean;
FilmCraft re-imported its own exports (self-consistency only).

| Format | Exported | Matches built timeline | What is lost / notable (FilmCraft's own export report + our checks) |
|---|---|---|---|
| **OTIO** (`otio`) | yes | **yes** (V1, V2, A1, A2 clips, in/out/duration, dissolve as `SMPTE_Dissolve` in 8/out 7, marker, durations; OTIO lib loads, 195 frames @30) | Title -> clip with `MissingReference` (name kept, text/style only in `metadata.filmcraft`); audio fade exported as a `SMPTE_Dissolve` transition at clip end; **`target_url` is relative to the .otio file** when media and export share a top-level dir |
| **FCP7 XML** (`xml`) | yes | **yes** (clipitems, start/end/in/out, files, transitionitem 97-112 center, Constant Power audio transition, marker) | Title -> `clipitem` without media (Source Text filter params carried); media URLs absolute `file://localhost/...`; clip gain folded into Audio Levels |
| **FCPXML** (`fcpxml` v1.10) | yes | partial (clips ok; **transition on non-V1 tracks dropped**: audio fade lost) | Title -> `asset-clip ref=""` (dangling ref, importers may reject); still asset gets a 29.97 `<format>` in a 30p sequence |
| **EDL** (`edl`, CMX 3600) | yes | V1 events + audio ok (src-in 15, dissolve `D 015`) | Only one video track (V2/title lost); no frame rate in file (FilmCraft's own re-import assumed 24 fps and got wrong durations -- the importer must be told 30); relative `SOURCE FILE`; transition source-in clamped (handle) |
| **AAF** (`aaf`) | yes | container magic only (no AAF library here); FilmCraft re-imports it with the right clips | Title left as a gap; "clip uses media before start of trimmed essence" warning |
| **OMF** (`omf`) | yes | non-empty file only | Audio only; markers not written |

Self re-import into FilmCraft: OTIO/FCP7/FCPXML/AAF reproduce clip order and times (OTIO brings the title back as an *offline* "Hello Title" clip; FCP7/FCPXML skip it).

Interchange gaps relevant to DESIGN section 13.4 / 15: **no format carries editable titles**. Plan: keep titles minimal, list them in `handoff_report.md`
(text, start, duration, track) and let the user recreate them in Resolve, or burn them only into the preview MP4.

## 7. Compatibility gaps and risks

1. Resolve import untested (biggest open risk). Recommended primary format: **OTIO**, then **FCP7 XML** as fallback; AAF/EDL only as last resort.
2. Relative media paths in OTIO/EDL (behaviour fixed in `crates/engine/src/interchange.rs`: `relative_to` = export dir; absolute only if no common top-level directory). Mitigation: put
   the handoff directory under a different top-level path than media, or post-process `target_url` to absolute and re-validate by reload; always ship `media_manifest.csv` (DESIGN 15.1).
3. Titles are not interchangeable (Section 6). Audio fades become dissolve transitions in OTIO -- verify behavior in Resolve before using fades in the handoff.
4. Center-aligned transitions need source handles on both sides; otherwise source-in is clamped (EDL report). The planner should keep handles >= half the transition or use end/start alignment (`sequence.setTransition align`).
5. Trim without ripple leaves gaps; use `mode:"ripple"` or place clips with computed positions. Placement of a movie creates linked V/A clips; `audio:false` for silent portrait inserts.
6. Mixed aspect: the 1080x1920 portrait clip is placed unscaled into a 1920x1080 sequence (FCPXML stores a different anchor); no scale-to-frame behavior was tested.
7. Alpha software: 710 commands, schema 15 and fast-moving; `docs/gaps.md` G15 states interchange is not validated in Avid/Pro Tools and XML import can be slow (97 s for a Premiere sequence, issue #461) -- not observed here (tiny files).
8. Headless MCP can read/write anywhere (no confinement) -- the adapter must validate every path. `media_import` requires absolute paths.
9. CPU render is ~15 fps at 1080p on 4 cores; use `scale` or a lower-res preset for QC previews on long edits. Hardware encoding was not tested.
10. Binaries: must be built from source unless release assets are confirmed (not verifiable here). Pin the commit/tag in the run manifest.

## 8. Manual Resolve test procedure (pending -- needs the user's Resolve)

Files: run the spike, then use `spike/out/interchange/phase0.otio`, `phase0.xml` (FCP7), `phase0.fcpxml`, `phase0.edl`, `phase0.aaf`, `spike/out/phase0.fcproj`, `spike/out/preview.mp4`. Media is in `tests/fixtures/generated/` (keep at the same absolute path).
Expected timeline: 1920x1080 30 fps, 195 frames (6 s 15 f); V1 landscape (src 0:00:00:15-4:00, 105 f) -> dissolve 15 f -> still 90 f; V2 title 0-60 then portrait 60-120; A1 landscape audio; A2 music, -6 dB, fade out; marker "Scene A" at frame 60.

1. In Resolve: create a project, set timeline frame rate to 30 and resolution 1920x1080 (Project Settings) *before* importing.
2. File > Import > Timeline > Import AAF, EDL, XML... and pick `phase0.otio` (OTIO import support depends on the Resolve version -- not verified here; record your version, and fall back to the FCP7 XML entry if OTIO is not offered). Leave "automatically import source clips" on, "set to timeline frame rate/resolution" on.
3. Record per format (otio, xml, fcpxml, edl, aaf): imports without error? Timeline length/fps; clip order and in/out (compare against the expected list); media online or needs relink (note whether relative paths in OTIO/EDL resolved); V1/V2/A1/A2 track structure; dissolve present and length; audio level (-6 dB) and fade; title (expect offline/missing or empty); portrait clip size/scale; marker position and name; audio sync by playing the first 4 s against `preview.mp4`.
4. Save screenshots of the Edit page and the import log for each format, and send them back with the Resolve version/edition.
5. Pass criterion for the pipeline: one format imports with correct order/in/out/duration/fps and online media; transitions and markers are bonus; titles are expected to fail.

## 9. Windows and macOS (DESIGN.md 22.1)

**Verified only on Linux in this spike.** Nothing below was run on Windows or macOS.

Documented by FilmCraft (README "Get started"/installation tables, `docs/releasing.md`, `packaging/`, `.github/workflows/`):

| Platform | Documented builds | Notes |
|---|---|---|
| Windows | x64, x86 and ARM64: `filmcraft-<v>-windows-<arch>.msi` and `-portable.zip` (code-signed; built with `+crt-static`, no VC++ redistributable) | MSI installs to `C:\Program Files\FilmCraft\` including `filmcraft-cli.exe` (not on `PATH`, `docs/agents.md`); `windows-arm64.yml` installs the ARM64 MSI on a Windows 11 ARM64 runner and runs `filmcraft-cli --version`. GUI draws via Vulkan/DX12 (`WGPU_BACKEND` override). Logs in `%APPDATA%\FilmCraft\Logs` |
| macOS | App `filmcraft-<v>-macos-universal.dmg` and **separate** CLI `filmcraft-cli-<v>-macos-universal.zip` (Apple silicon + Intel, signed and notarized) | The `.app` holds only the desktop app, so the headless MCP/CLI must come from the zip (or a source build). Not on Homebrew as far as the docs say |
| Linux | AppImage, Flatpak, deb, rpm, tar.gz for x86_64 and aarch64; riscv64 tarball | deb/rpm put the CLI at `/usr/bin/filmcraft-cli` |

Caveats: the README says Windows and Linux get "far less testing than macOS" and hardware decode/encode differs per OS (VideoToolbox, Media Foundation, VA-API, NVENC);
`docs/gaps.md` lists Windows/Linux run-time behaviour as an open gap (G9). The repo's own `ci.yml` delegates to a shared workflow in another repository, so which OSes
its test gate covers is **not visible** from the checkout; `release.yml` / `docs/releasing.md` show macOS and Windows runners for packaging. Whether the headless `mcp` mode behaves
identically on Windows/macOS (path forms, `file://` URL style, relative-path rule in OTIO/EDL, `file.exportMedia` presets) is **untested** -- the spike must be re-run there and the snapshot compared.

Spike portability: `spike/phase0_filmcraft_spike.py` uses `pathlib`, argument-list subprocesses (no shell), binary pipes with UTF-8 and `\n`, normalises Windows drive-letter `file:///C:/...` paths for
comparison, and discovers `filmcraft-cli` (`.exe` on Windows) via `--filmcraft-cli`, `$FILMCRAFT_CLI`, `PATH`, a sibling source build, `Program Files\FilmCraft`, Homebrew prefixes
and `/usr/bin`. `tests/fixtures/make_fixtures.py` is the cross-platform fixture generator (`make_fixtures.sh` remains as a POSIX convenience). Windows/macOS code paths are unexecuted.

## 10. Files

* `spike/phase0_filmcraft_spike.py` -- end-to-end spike (stdlib; optional otio/xmllint); `--filmcraft-cli`, `--filmcraft-src`, `--fixtures`, `--out`; exit 0 / 1 (required check failed) / 2 (usage).
* `tests/fixtures/make_fixtures.py` (and `.sh`) -- ffmpeg fixtures (5 s 1080p30 landscape + tone, 4 s 1080x1920 portrait, 1080p PNG, 6 s wav and m4a); output git-ignored.
* `docs/phase0/capability_snapshot.json`, `docs/phase0/mcp_tools.json` -- evidence. Spike run output (`commands.jsonl` sanitized log, exports, preview) goes to `spike/out/` (git-ignored).
