# Verifying the Resolve handoff

Resolve cannot run in CI, so handoff fidelity is only claimed for what you verify on your own machine.
`tools/resolve_verify.py` (standard library only, Python 3.10+) imports the interchange files into a
scratch Resolve project, reads the imported timeline back through Resolve's scripting API and compares it
with the expected timeline. If scripting is unavailable it prints the manual checklist (below) and exits 3.

## Prerequisites

* Windows 10/11 or macOS 13+, with DaVinci Resolve **running** (a project manager window is enough).
* External scripting enabled: **Preferences > System > General > External scripting using = Local**, then restart Resolve.
  Studio supports this; the free edition may not (then the script reports it and you use the manual checklist).
* Resolve 18 or newer recommended. `.otio` import is a newer feature (it is offered in 18.5+ / 19); the script tries every file you give it and
  reports per format, so an unsupported format simply shows as a failed import. FCP7 `.xml` is the fallback.
* Python 3.10+ on the same machine. No packages needed.
* Save your open Resolve work first: the script creates a scratch project, which closes the current one. It never modifies or deletes
  existing projects, never touches media, and deletes only the scratch project it created (use `--keep-project` to keep it).
* Media must be at the same absolute paths as when the draft was built.

Script discovery follows Blackmagic's documented locations and honours `RESOLVE_SCRIPT_API` and `RESOLVE_SCRIPT_LIB`:

| OS | API folder (`<api>/Modules` is added to `sys.path`) | Library |
|---|---|---|
| macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting` | `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so` |
| Windows | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` | `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll` |

## Running it

macOS (Terminal):

```bash
# Phase 0 spike outputs (spike/out/interchange/phase0.*; expected timeline is derived from phase0.otio)
python3 tools/resolve_verify.py --spike-out spike/out

# A draft's handoff folder
python3 tools/resolve_verify.py --handoff "/path/to/workspace/resolve_handoff"
```

Windows (PowerShell):

```powershell
python tools\resolve_verify.py --spike-out spike\out
python tools\resolve_verify.py --handoff "D:\Trips\Iceland\resolve_handoff"
```

Other options: `--timeline FILE` (repeatable), `--expected FILE`, `--output PATH` (default `<handoff>/resolve_verification.json`,
else the current directory), `--keep-project`. If the env vars point elsewhere (non-default install), set them first, e.g.
`export RESOLVE_SCRIPT_API=...` / `$env:RESOLVE_SCRIPT_LIB="..."`.

Exit codes: `0` at least one format passed all required checks; `1` none did; `2` bad inputs; `3` scripting unavailable (manual required).

Expected timeline sources, in order: `--expected FILE`; `expected_timeline.json` in the handoff dir; otherwise derived from the first `.otio`
file by a built-in stdlib OTIO reader (no `opentimelineio` needed). Relative `target_url`s are resolved against the `.otio` file's folder.

### Expected-timeline JSON

```json
{
  "name": "Main", "fps": 30, "width": 1920, "height": 1080, "duration_frames": 195, "transitions": 2,
  "markers": [{"name": "Scene A", "frame": 60}],
  "tracks": [
    {"name": "Video 1", "kind": "video", "clips": [
      {"name": "landscape_5s.mp4", "source_path": "/abs/landscape_5s.mp4",
       "record_start_frame": 0, "record_end_frame": 105, "source_in_frame": 15, "source_out_frame": 120}
    ]}
  ]
}
```

Frames are integers at `fps`; `record_end_frame` is exclusive. `kind` is `video` or `audio`. A clip with `"source_path": null`
(a title or graphic) is optional and reported as info only. Paths may be plain, Windows (`C:\...`) or `file://` forms.

## What each check means

Required checks decide the exit code (a format passes when all of them pass); others are reported but do not fail the run.

| Check | Required | Meaning |
|---|---|---|
| import | yes | `MediaPool.ImportTimelineFromFile` returned a timeline (otherwise this Resolve cannot import that format) |
| fps | yes | timeline frame rate equals expected (set on the scratch project before import) |
| duration | yes | timeline end minus start equals expected, +/-1 frame |
| clip_order | yes | per track (V1, V2, A1, ...), media clips appear in the expected order with matching names/files |
| record_inout | yes | each clip's timeline start/end within +/-1 frame |
| source_in | yes | each clip's source in-point within +/-1 frame (stills skipped) |
| media_online | yes | each clip has a media pool item whose file path exists on disk (otherwise offline / needs relink) |
| resolution | no | timeline width x height |
| track_structure | no | number of populated video and audio tracks |
| media_path | no | Resolve's file path equals expected (case-insensitive on Windows/macOS, `file://` and relative forms normalised) |
| markers | no | marker count and frame positions (+/-1) |
| transitions | info | Resolve's scripting API exposes almost nothing about transitions; check dissolves and fades on the Edit page |
| titles | info | whether title/graphic clips arrived; FilmCraft titles are expected to be offline or empty (no format carries editable titles) |

## Sending results back

Send `resolve_verification.json` (contains Resolve product, version, OS, per-format per-check results, the raw dump of each imported
timeline) and the console table, plus screenshots of the Edit page for any format with a failed or `info` check. When `status` is
`manual_required`, fill in the checklist below and send that with your Resolve version/edition (Help > About).

## Manual fallback checklist

Files: `phase0.otio`, `phase0.xml` (FCP7), `phase0.fcpxml`, `phase0.edl`, `phase0.aaf`, `preview.mp4`; media at its original absolute path.
For the spike timeline: 1920x1080 30 fps, 195 frames; V1 landscape (source from frame 15, 105 f) -> 15 f dissolve -> still 90 f;
V2 title 0-60 then portrait 60-120; A1 landscape audio; A2 music at -6 dB with fade out; marker "Scene A" at frame 60.

1. Create a project; Project Settings: timeline frame rate and resolution = expected values, **before** importing.
2. File > Import > Timeline > Import AAF, EDL, XML... and pick each file (`.otio` if offered, else the `.xml`). Leave "automatically import source clips"
   and "set to timeline frame rate/resolution" on.
3. For each format record: imports without error; timeline length and fps; clip order and in/out against the expected list; media online or
   needs relinking (relative paths in OTIO/EDL); V1/V2/A1/A2 track structure; dissolve present and its length; audio level and fade;
   title (expect offline or empty); portrait clip size/scale; marker position and name; audio sync of the first 4 s against `preview.mp4`.
4. Screenshot the Edit page and the import log per format.
5. Pass: one format has correct order, in/out, duration, fps and online media. Transitions and markers are bonus; titles are expected to fail.
