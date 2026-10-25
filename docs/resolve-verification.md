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
else the current directory), `--keep-project`, `--include-risky-formats`, `--experiments` (force the still-sequence experiments below). If the env vars point elsewhere (non-default install), set them first, e.g.
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

### Which formats are tried

By default only FCP7 `.xml` and then `.otio` are imported. FCPXML, EDL and AAF were observed to crash (FCPXML), freeze (EDL) or fail
(AAF "cannot be opened for reading") on Resolve 21.1, so they are reported as `info: skipped` unless you pass `--include-risky-formats`.
Even then they run last (EDL, AAF, FCPXML very last) and the script warns you to save your work first; if Resolve dies, the safe formats
were already imported but the report is only written at the end, so prefer running the default first.

Every path handed to Resolve is absolute with native separators (Resolve runs in another process, so relative paths silently fail).
Each import uses unique timeline names (`verify_<run stamp>_<format>`; reusing a name makes Resolve return None silently) and the options
`importSourceClips=True` plus `sourceClipsPath` = the folder that holds the expected media. If Resolve returns None, the script retries once
with just the path (no options); both attempts are recorded under `attempts` in the JSON.

### OTIO media-path variants

The spike's `phase0.otio` has relative `target_url`s, which Resolve does not link. Before importing an `.otio` the script writes up to three
copies under `<output folder>/resolve_verify_work_<stamp>/otio_<variant>/` in which only relative (or otherwise unresolvable) `target_url`s are
rewritten to absolute media references; everything else is unchanged. Each is imported and checked separately:

| Column | `target_url` form | Example |
|---|---|---|
| `otio[original]` | unchanged | `../../../tests/fixtures/generated/x.mp4` |
| `otio[bare]` | native path (what Resolve itself exports) | `C:\Users\me\repo\tests\fixtures\generated\x.mp4` |
| `otio[slash]` | forward slashes | `C:/Users/me/repo/tests/fixtures/generated/x.mp4` |
| `otio[file_url]` | percent-encoded URL | `file:///C:/Users/me/repo/tests/fixtures/generated/x.mp4` |

The run shows which form Resolve links (media_online passes). If an OTIO already has only absolute references, no variants are made.

On Resolve Studio 21.1 `ImportTimelineFromFile` returned None for the original and all three variants (with and without options), although
a manual File > Import > Timeline of the original works (media missing). The OTIO result therefore reads "rejected by ImportTimelineFromFile
in this Resolve version (manual File > Import > Timeline may still work)". The variants stay in the work folder; the script prints a one-line
suggestion to import `<work>/otio_bare/phase0.otio` through the UI and tell us whether the media links.

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
| image_sequence_misdetected | yes | no expected still was turned into an image sequence (see below) |
| resolution | no | timeline width x height |
| track_structure | no | number of populated video and audio tracks |
| media_path | no | Resolve's file path equals expected (case-insensitive on Windows/macOS, `file://` and relative forms normalised) |
| markers | no | marker count and frame positions (+/-1) |
| transitions | no (pass/fail) | expected count equals the transitions found (see below); each one is listed with track, start, end and centre relative to the nearest cut |
| titles | info | whether title/graphic clips arrived; FilmCraft titles are expected to be offline or empty (no format carries editable titles) |

## Transitions

`GetItemListInTrack` returns transitions as ordinary items without a media pool item. The script marks an item `"kind": "transition"` (raw item
kept in the dump) when it has no media pool item and either its name contains Dissolve, Wipe, Dip, Cross Fade, Fade or Transition (or is
"Final Cut Pro 7", which is how Resolve labels imported FCP7 audio transitions), or it is short (<= 30 frames) and spans or abuts a cut or clip
end of its track. Names that look like media files are never transitions. Transitions are left out of `clip_order`, `record_inout` and the media
checks and counted for `transitions`. Titles and generators without media that are not transitions behave as before (info only). Example from a
real run: `V1 'Cross Dissolve' 97-112 centre 104.5 (nearest cut 105, offset -0.5)` and `A2 'Final Cut Pro 7' 165-180` (the audio fade-out).

## The image-sequence problem

Resolve reads digits in a still's file name as a frame number: `still_1080p.png` was imported as `still_[1080-1229]p.png`, an image sequence
whose file does not exist, so the clip is offline. The script detects an item whose name or path has a `[N-M]` bracket where the text around
it plus a number inside the range equals an expected file (`IMG_1042.JPG` -> `IMG_[1042-1100].JPG`), or, failing that, has the expected image
extension at the expected record range. It reports the required check `image_sequence_misdetected` naming both files and matches the item to
the expected clip, so order and record in/out are still evaluated (record 105-195 passes). `media_online` stays honest: it fails while the
still is offline in the original import.

### Experiments (`experiments.still_sequence` in the JSON)

Run automatically for the FCP7 XML when a misdetection is found, or always with `--experiments`. They only add timelines and media to the
scratch project (nothing is deleted, original files are never touched); each is wrapped so a failure is recorded, never fatal. Each approach
lists every call with arguments and result, whether the still is online, its name and path, and `works` (online, no bracket name, order and
record range correct):

1. `import_source_clips_no_path`: importSourceClips=True without `sourceClipsPath`.
2. `pre_import_media_then_xml`: `MediaPool.ImportMedia([path])` once per expected file (returned clip names recorded), then importSourceClips=False.
3. `pre_import_media_dict_then_xml`: same with `ImportMedia([{"FilePath": path}])` if supported.
4. `repair_replace_clip`: on the original import, the misdetected item's `MediaPoolItem.ReplaceClip(expected path)`, then `MediaPool.RelinkClips([item], folder)`
   if that fails; the item is dumped before and after.

The console summarises which approach gives an online still with the correct record range and `recommended` holds the first that works. Imports
share the scratch media pool, so earlier approaches can influence later ones (approach 2 runs before 3).

## Sending results back

Send `resolve_verification.json` and the console table (including the experiments summary and the OTIO manual-import line). If you tried the
`<work>/otio_bare/phase0.otio` manual import, say whether the media linked. Per format it records the absolute path passed, whether the file exists, its size, the
type Resolve returned, and the `attempts` list. Cleanup details are in `cleanup_steps`. If every import returned None, also open Resolve's
**Workspace > Console** right after the run and copy what it printed (it often names the import error). If the scratch project could not be
deleted, the script prints its exact name (`tripvlog_verify_<stamp>`) and manual steps: open the Project Manager, make sure another project is
open, right-click the scratch project > Delete. The script deletes only the project it created.

The JSON also contains the Resolve product, version, OS, per-format per-check results, the raw dump of each imported
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
