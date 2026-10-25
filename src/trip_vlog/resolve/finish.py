"""``trip-vlog resolve-finish``: finish the Resolve handoff inside DaVinci Resolve Studio (DESIGN 15.6).

A deterministic, allowlisted adapter (no LLM, no MCP layer, no eval/exec) that drives Resolve's documented scripting
API through :mod:`trip_vlog.resolve.guard`:

1. create a NEW, uniquely named Resolve project (an existing project is never reused, modified or deleted) and set
   frame rate and resolution from the edit plan;
2. import ``resolve_handoff/timeline.xml`` (FCP7 XML) with a unique timeline name;
3. repair media that arrived offline or was misread as an image sequence (``IMG_1042.JPG`` -> ``IMG_[1042-1100].JPG``)
   with ``MediaPoolItem.ReplaceClip(<path from media_manifest.csv>)``; a still that Resolve refuses because it has numbered siblings
   in its folder falls back to an isolated copy under ``resolve_handoff/stills/<key>/``;
4. add scene markers that are missing;
5. try to create the titles of the timeline spec as editable Resolve titles (EXPERIMENTAL; anything that cannot be
   verified by read-back is rolled back and listed for manual recreation);
6. read the timeline back, compare it with the FilmCraft timeline spec and write
   ``resolve_handoff/resolve_finish_report.json`` / ``.md``.

Exit codes: 0 finished (see the report for titles that need manual work), 1 required checks failed,
2 preconditions or arguments, 3 Resolve scripting unavailable (diagnosis printed, nothing changed).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trip_vlog import __version__
from trip_vlog.filmcraft.execution import load_journal
from trip_vlog.filmcraft.timeline_spec import TimelineSpec, TitleSpec, downgrade_dissolves, parse_track
from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.media.scan import IMAGE_EXTS as STILL_EXTS
from trip_vlog.planning.duration import parse_fps
from trip_vlog.resolve.api import method, safe_call
from trip_vlog.resolve.compare import align_tracks, compare_timeline, format_passed, match_sequence, _r
from trip_vlog.resolve.discovery import connect_resolve, python_compat_hint, python_info
from trip_vlog.resolve.dump import SEQ_RE, TOL_FRAMES, dump_item, dump_timeline
from trip_vlog.resolve.expected import count_xml_transitions, expected_from_spec
from trip_vlog.resolve.guard import MUTATING, ApiSession, media_key, media_keys, stills_key
from trip_vlog.resolve.paths import common_media_dir, native_path
from trip_vlog.state import Manifest, Workspace, atomic_write_json, atomic_write_text

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3
STAGE = "resolve_finish"
REPORT_JSON = "resolve_finish_report.json"
REPORT_MD = "resolve_finish_report.md"
STILLS_DIR = "stills"  # resolve_handoff/stills/<12 hex>/<original name>: isolated copies of stills (no numbered siblings)
SCHEMA_VERSION = 1
MARKER_COLOR = "Blue"
TITLE_ROUTES = (("InsertFusionTitleIntoTimeline", "Text+"), ("InsertTitleIntoTimeline", "Text"))
TITLE_TEXT_TOOLS = (("FindToolByID", "TextPlus"), ("FindTool", "Template"))
# API calls observed to work on the user's Resolve Studio 21.1.1.10 (Windows 11) through tools/resolve_verify.py.
PROVEN_CALLS = frozenset({
    "resolve.GetProductName", "resolve.GetVersionString", "resolve.GetVersion", "resolve.GetProjectManager",
    "pm.GetCurrentProject", "pm.GetCurrentFolder", "pm.CreateProject", "project.GetName", "project.SetSetting",
    "project.GetMediaPool", "project.SetCurrentTimeline", "mp.ImportTimelineFromFile", "mpi.ReplaceClip",
    "mpi.GetClipProperty", "timeline.GetName", "timeline.GetStartFrame", "timeline.GetEndFrame", "timeline.GetSetting",
    "timeline.GetTrackCount", "timeline.GetItemListInTrack", "timeline.GetMarkers", "item.GetName", "item.GetStart",
    "item.GetEnd", "item.GetDuration", "item.GetLeftOffset", "item.GetMediaPoolItem",
})

# Test hook: replace to inject a fake Resolve, ``lambda: (resolve_or_None, info_dict)``.
resolve_factory: Callable[[], tuple[Any, dict]] | None = None


class FinishError(Exception):
    """A precondition failed (exit code 2); the message says what to do."""


# ---------------------------------------------------------------------------------------------- workspace inputs
@dataclass
class FinishInputs:
    root: Path
    handoff: Path
    xml_path: Path
    spec: TimelineSpec  # effective spec of the built FilmCraft project (downgraded dissolves applied)
    plan: dict[str, Any]
    run: dict[str, Any]  # journal entry of the built project version
    rows: list[dict[str, str]]
    project_title: str
    xml_sha256: str
    warnings: list[str] = field(default_factory=list)

    @property
    def used_rows(self) -> list[dict[str, str]]:
        return [r for r in self.rows if r.get("used_in_timeline") == "yes"]

    @property
    def manifest_paths(self) -> list[str]:
        return [r["absolute_path"] for r in self.rows]

    @property
    def media_dir(self) -> str | None:
        return common_media_dir(r["absolute_path"] for r in self.used_rows)

    @property
    def plan_markers(self) -> list[dict[str, Any]]:
        return [{"name": m["name"], "frame": int(m["frame"]), "comment": m.get("scene_id") or ""}
                for m in (self.plan.get("markers") or [])]


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FinishError(f"cannot read {what} ({path}): {e}") from None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_inputs(output: Path | str) -> FinishInputs:
    """Read and cross-check everything resolve-finish needs from the workspace. Raises FinishError."""
    ws = Workspace(Path(output).expanduser().resolve())
    if not ws.manifest_path.is_file():
        raise FinishError(f"{ws.root} is not a trip-vlog workspace (no manifest.json); run `trip-vlog draft` first")
    m = Manifest.load(ws.manifest_path)
    if m.get("pipeline") != "draft":
        raise FinishError("this workspace has no draft; run `trip-vlog draft` first")
    handoff_stage = m.stage("handoff")
    if handoff_stage.get("status") not in ("succeeded", "partial"):
        raise FinishError(f"the handoff stage has not completed (status: {handoff_stage.get('status', 'pending')}); "
                          f"run `trip-vlog resume --output {ws.root}` first")
    hd = ws.root / "resolve_handoff"
    xml = hd / "timeline.xml"
    needed = {"timeline.xml": xml, "media_manifest.csv": hd / "media_manifest.csv", "edit_plan.json": hd / "edit_plan.json",
              "project_reference.json": hd / "project_reference.json"}
    missing = [n for n, p in needed.items() if not p.is_file()]
    if missing:
        hint = " (FCP7 XML export is required; check filmcraft.interchange_formats and handoff_report.md)" if "timeline.xml" in missing else ""
        raise FinishError(f"resolve_handoff/ is incomplete, missing: {', '.join(missing)}{hint}")
    fc = m.get("filmcraft") or {}
    version = fc.get("project_version")
    journal = load_journal(ws.root / "filmcraft" / "execution_journal.json")
    run = next((r for r in journal["runs"] if r.get("version") == version and r.get("status") == "complete"
                and r.get("project_path") == fc.get("project")), None)
    if run is None:
        raise FinishError("no completed FilmCraft project version found in filmcraft/execution_journal.json "
                          f"(manifest says v{version}); run `trip-vlog resume --output {ws.root}`")
    ref = _read_json(needed["project_reference.json"], "project_reference.json")
    if ref.get("project_version") != version:
        raise FinishError(f"resolve_handoff/ was exported from FilmCraft project v{ref.get('project_version')} but the workspace "
                          f"is at v{version}; run `trip-vlog resume --output {ws.root}` to refresh the handoff")
    spec_file = ws.root / str(run.get("spec_file") or "")
    if not spec_file.is_file():
        raise FinishError(f"the timeline spec of project v{version} is missing: {spec_file}")
    spec = TimelineSpec.from_dict(_read_json(spec_file, "timeline spec"))
    if spec.spec_hash() != run.get("spec_hash"):
        raise FinishError(f"{spec_file.name} does not match the hash recorded in the execution journal; it was modified")
    spec = downgrade_dissolves(spec, run.get("downgraded_dissolves") or [])
    plan = _read_json(needed["edit_plan.json"], "edit_plan.json")
    seq = plan.get("sequence") or {}
    try:
        plan_fps = parse_fps(seq.get("fps"))
    except ValueError as e:
        raise FinishError(f"edit_plan.json has no usable sequence.fps: {e}") from None
    if (plan_fps, seq.get("width"), seq.get("height")) != (spec.fps, spec.width, spec.height):
        raise FinishError("edit_plan.json (sequence fps/size) disagrees with the FilmCraft timeline spec; "
                          f"run `trip-vlog resume --output {ws.root}`")
    with open(needed["media_manifest.csv"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "absolute_path" not in rows[0]:
        raise FinishError("media_manifest.csv is empty or malformed")
    keys = media_keys(r["absolute_path"] for r in rows)
    outside = [p for p in spec.asset_paths if media_key(p) not in keys]
    if outside:
        raise FinishError(f"timeline uses media that is not in media_manifest.csv: {outside[0]}")
    warnings = [f"used media is missing on disk: {r['absolute_path']}" for r in rows
                if r.get("used_in_timeline") == "yes" and not Path(r["absolute_path"]).is_file()]
    title = str((plan.get("project") or {}).get("title") or "Trip Vlog")
    return FinishInputs(ws.root, hd, xml, spec, plan, run, rows, title, _sha256(xml), warnings)


# ---------------------------------------------------------------------------------------------- naming and plan
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def project_base_name(title: str) -> str:
    return _BAD_NAME_CHARS.sub(" ", title).strip(" .") or "Trip Vlog"


def next_project_name(title: str, existing: list[str], recorded: list[str] = ()) -> tuple[str, int]:
    """``<title> (trip-vlog vNNN)`` with NNN one above the highest version seen in Resolve or in the manifest."""
    base = project_base_name(title)
    pat = re.compile(rf"^{re.escape(base)} \(trip-vlog v(\d+)\)$")
    seen = [int(m[1]) for n in [*existing, *recorded] if (m := pat.match(n))]
    n = max(seen, default=0) + 1
    return f"{base} (trip-vlog v{n:03d})", n


def fps_setting(fps: Any) -> str:
    """Value for the project setting timelineFrameRate (Resolve spells NTSC rates 23.976 / 29.97 / 59.94)."""
    f = float(fps)
    for ntsc in (23.976, 29.97, 47.952, 59.94, 119.88):
        if abs(f - ntsc) < 0.005:
            return f"{ntsc:g}"
    return f"{f:g}"


def planned_steps(inp: FinishInputs, project_name: str | None) -> list[str]:
    spec = inp.spec
    media = inp.media_dir
    return [
        f"create a NEW Resolve project '{project_name or '<chosen at run time>'}' (existing projects are never modified or deleted)",
        f"set timelineFrameRate={fps_setting(spec.fps)}, timelineResolution={spec.width}x{spec.height} (from edit_plan.json), before import",
        f"import {inp.xml_path.name} (FCP7 XML, {len(spec.placements)} placement(s)) with importSourceClips=True"
        + (f" and sourceClipsPath={native_path(media)}" if media else ""),
        "read the timeline back; for every media clip that is offline or was misread as an image sequence (e.g. IMG_1042.JPG), "
        "call MediaPoolItem.ReplaceClip(<its path from media_manifest.csv>), then confirm it is online; if Resolve refuses a still "
        "(it sees numbered siblings in its folder as an image sequence), copy that one file to "
        "resolve_handoff/stills/<key>/<name> (the original is never touched) and ReplaceClip to the copy",
        f"add {len(inp.plan_markers)} scene marker(s) that are missing (existing frame+name matches are not duplicated)",
        (f"EXPERIMENTAL: create {len(spec.titles)} title(s) (Text+ in a helper timeline, appended on the intended track and range, "
         "text set through the Fusion comp); a title that cannot be verified is rolled back and listed" if spec.titles
         else "titles: none in this timeline spec"),
        "read the timeline back, compare it with the FilmCraft timeline spec (fps, duration, clip order, record/source in-out "
        f"+/-{TOL_FRAMES} frame, media online, markers, transitions, titles), save the project",
        f"write resolve_handoff/{REPORT_JSON} and {REPORT_MD}",
    ]


# ---------------------------------------------------------------------------------------------- run state
@dataclass
class FinishRun:
    inp: FinishInputs
    api: ApiSession
    stamp: str
    keep_on_failure: bool
    phases: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    echo: Callable[[str], None] = print
    staged: dict[str, str] = field(default_factory=dict)  # isolated copy -> expected source path (for the final comparison)

    def phase(self, name: str, status: str, detail: str = "", **extra: Any) -> dict[str, Any]:
        rec = {"phase": name, "status": status, "detail": detail, **extra}
        self.phases.append(rec)
        self.echo(f"  [{status:<7}] {name}" + (f": {detail}" if detail else ""))
        return rec


def _item_at(tl: Any, kind: str, tindex: int, index: int) -> Any:
    items = safe_call(method(tl, "GetItemListInTrack"), kind, tindex) or []
    return items[index] if isinstance(index, int) and 0 <= index < len(items) else None


def _is_bad_media(item: dict[str, Any]) -> bool:
    return (not item.get("online")) or bool(SEQ_RE.match(str(item.get("name") or ""))) \
        or bool(SEQ_RE.match(os.path.basename(str(item.get("media_path") or "").replace("\\", "/"))))


# ---------------------------------------------------------------------------------------------- repairs
STILLS_DIR_REL = f"resolve_handoff/{STILLS_DIR}"


def stage_still(run: FinishRun, source: str) -> Path:
    """Copy a still to ``resolve_handoff/stills/<key>/<name>`` (shutil.copy2, never a link) and return the copy. The folder holds only
    that file, so Resolve sees no numbered siblings. An existing copy of the same size is reused. The source is only read."""
    src = Path(source)
    dest = run.inp.handoff / STILLS_DIR / stills_key(source) / src.name
    if dest.is_file() and dest.stat().st_size == src.stat().st_size:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _redump(tl: Any, pair: dict[str, Any], it: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    live = _item_at(tl, pair["kind"], pair["tindex"], it.get("index"))
    return dump_item(live, actual.get("start_frame") or 0, []) if live is not None else {}


def repair_media(run: FinishRun, tl: Any, expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """ReplaceClip every offline / image-sequence-misread media clip whose expected source is in the manifest and on disk."""
    allowed = run.api.allowed_media
    repairs: list[dict[str, Any]] = []
    for pair in align_tracks(expected, actual)["pairs"]:
        for j, clip in enumerate(pair["required"]):
            if j >= len(pair["rest"]):
                continue
            it = pair["rest"][j]
            seq = match_sequence(clip, it)
            if it.get("online") and not seq:
                continue
            rec: dict[str, Any] = {"track": pair["label"], "position": j, "expected_path": clip["source_path"],
                                   "was": {k: it.get(k) for k in ("name", "media_path", "online")},
                                   "reason": "image sequence misread" if seq else "offline", "action": "ReplaceClip"}
            repairs.append(rec)
            path = clip["source_path"]
            if media_key(path) not in allowed:
                rec.update(result="refused", detail="path is not listed in media_manifest.csv")
                continue
            if not os.path.isfile(path):
                rec.update(result="skipped", detail="the expected file does not exist on disk")
                continue
            live = _item_at(tl, pair["kind"], pair["tindex"], it.get("index"))
            if live is None:
                rec.update(result="skipped", detail="timeline item not found again")
                continue
            before = dump_item(live, actual.get("start_frame") or 0, [])
            if not _is_bad_media(before):
                rec.update(result="already_online", detail="fixed by an earlier repair of the same media pool item")
                continue
            mpi = safe_call(method(live, "GetMediaPoolItem"))
            if mpi is None:
                rec.update(result="skipped", detail="the timeline item has no media pool item")
                continue
            attempts: list[dict[str, Any]] = []
            rec["attempts"] = attempts
            try:
                ok = method(mpi, "ReplaceClip")(native_path(path))
            except Exception as e:  # noqa: BLE001 - recorded, never fatal
                rec.update(result="error", detail=f"{type(e).__name__}: {e}")
                continue
            attempts.append({"path": native_path(path), "returned": bool(ok)})
            after = _redump(tl, pair, it, actual)
            fixed = bool(after) and not _is_bad_media(after)
            if not fixed and os.path.splitext(path)[1].lower() in STILL_EXTS:
                # Observed: ReplaceClip refuses a file that has numbered siblings in its folder (Resolve sees one frame of an
                # image sequence). Retry with an isolated single-file copy; the original is only read.
                try:
                    staged = stage_still(run, path)
                    run.api.allow_staged(path, staged)
                    rec["staged_copy"] = str(staged)
                    ok = method(mpi, "ReplaceClip")(native_path(staged))
                    attempts.append({"path": native_path(staged), "returned": bool(ok)})
                    after = _redump(tl, pair, it, actual)
                    fixed = bool(after) and not _is_bad_media(after)
                    if fixed:
                        run.staged[str(staged)] = path
                        rec["detail"] = (f"the clip now references the isolated copy {STILLS_DIR_REL}/{staged.parent.name}/"
                                         f"{staged.name} (Resolve refused the original, which has numbered siblings)")
                except Exception as e:  # noqa: BLE001
                    rec.update(result="error", detail=f"isolated copy fallback failed: {type(e).__name__}: {e}")
                    continue
            rec.update(result="online" if fixed else "still_offline", replace_returned=bool(ok),
                       now={k: after.get(k) for k in ("name", "media_path", "online", "start", "end")})
    return repairs


# ---------------------------------------------------------------------------------------------- markers
def sync_markers(run: FinishRun, tl: Any, wanted: list[dict[str, Any]], actual: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    existing = actual.get("markers") or []
    for m in wanted:
        rec: dict[str, Any] = {"name": m["name"], "frame": m["frame"]}
        out.append(rec)
        same_frame = [e for e in existing if e["frame"] == m["frame"]]
        if any((e.get("name") or "") == m["name"] for e in same_frame):
            rec["result"] = "present"
            continue
        if same_frame:
            rec.update(result="conflict", detail=f"another marker '{same_frame[0].get('name')}' is already at this frame; not added")
            continue
        try:
            ok = method(tl, "AddMarker")(m["frame"], MARKER_COLOR, m["name"], m.get("comment") or "", 1, "")
        except Exception as e:  # noqa: BLE001
            rec.update(result="error", detail=f"{type(e).__name__}: {e}")
            continue
        rec["result"] = "added" if ok else "rejected"
        if not ok:
            rec["detail"] = "AddMarker returned False"
    return out


# ---------------------------------------------------------------------------------------------- titles (experimental)
def _find_item(dump: dict[str, Any], kind: str, tindex: int, name: str, start: int, end: int) -> dict[str, Any] | None:
    for t in dump["tracks"]:
        if t["kind"] == kind and t["index"] == tindex:
            for it in t["items"]:
                if it.get("name") == name and it.get("start") is not None and abs(it["start"] - start) <= TOL_FRAMES \
                        and abs(it["end"] - end) <= TOL_FRAMES:
                    return it
    return None


def _set_title_text(item: Any, text: str, steps: list[str]) -> bool:
    count = safe_call(method(item, "GetFusionCompCount"))
    steps.append(f"GetFusionCompCount -> {count!r}")
    if not count:
        return False
    comp = safe_call(method(item, "GetFusionCompByIndex"), 1)
    if comp is None:
        steps.append("GetFusionCompByIndex(1) -> None")
        return False
    for finder, tool_id in TITLE_TEXT_TOOLS:
        tool = safe_call(method(comp, finder), tool_id)
        steps.append(f"{finder}({tool_id!r}) -> {'tool' if tool is not None else None}")
        if tool is None:
            continue
        r = safe_call(method(tool, "SetInput"), "StyledText", text)
        back = safe_call(method(tool, "GetInput"), "StyledText")
        steps.append(f"SetInput('StyledText') -> {r!r}; GetInput -> {back!r}")
        if back == text:
            return True
    return False


def create_title(run: FinishRun, project: Any, mp: Any, tl: Any, index: int, title: TitleSpec, tl_start: float) -> dict[str, Any]:
    """EXPERIMENTAL. Title in its own helper timeline (Text+), appended to the main timeline on the intended track and
    range with MediaPool.AppendToTimeline, text set through the Fusion comp, verified by read-back. Anything that fails
    or cannot be verified is undone (only items/timelines this run created) and reported."""
    rec: dict[str, Any] = {"index": index, "text": title.text, "track": title.track, "start_frame": title.start_frame,
                           "duration_frames": title.duration_frames, "experimental": True, "steps": [], "status": "failed",
                           "helper_timeline": f"trip-vlog title {index + 1} [{run.stamp}]", "applied": [],
                           "not_applied": [x for x in (("size" if title.size else None), ("position" if title.position else None)) if x]}
    steps: list[str] = rec["steps"]
    helper: Any = None
    appended: list[Any] = []
    tindex = parse_track(title.track)[1]
    try:
        helper = safe_call(method(mp, "CreateEmptyTimeline"), rec["helper_timeline"])
        steps.append(f"CreateEmptyTimeline({rec['helper_timeline']!r}) -> {'timeline' if helper is not None else None}")
        if helper is None:
            raise RuntimeError("CreateEmptyTimeline returned None")
        safe_call(method(project, "SetCurrentTimeline"), helper)
        item = None
        for route, tname in TITLE_ROUTES:
            item = safe_call(method(helper, route), tname)
            steps.append(f"{route}({tname!r}) -> {'item' if item is not None else None}")
            if item is not None:
                rec["route"] = route
                break
        if item is None:
            raise RuntimeError("neither InsertFusionTitleIntoTimeline nor InsertTitleIntoTimeline produced a title")
        if not _set_title_text(item, title.text, steps):
            raise RuntimeError("the title text could not be set and read back through the Fusion comp")
        rec["applied"].append("text")
        h_start = safe_call(method(helper, "GetStartFrame")) or 0
        h_end = safe_call(method(helper, "GetEndFrame"))
        if h_end is None or (h_end - h_start) < title.duration_frames - TOL_FRAMES:
            raise RuntimeError(f"the title template is only {None if h_end is None else h_end - h_start} frames long, "
                               f"{title.duration_frames} are needed")
        root = safe_call(method(mp, "GetRootFolder"))
        clips = safe_call(method(root, "GetClipList")) or [] if root is not None else []
        mpi = next((c for c in clips if safe_call(method(c, "GetName")) == rec["helper_timeline"]), None)
        steps.append(f"find the helper timeline in the media pool -> {'found' if mpi is not None else 'not found'}")
        if mpi is None:
            raise RuntimeError("the helper timeline was not found in the media pool root folder")
        safe_call(method(project, "SetCurrentTimeline"), tl)
        have = safe_call(method(tl, "GetTrackCount"), "video") or 0
        while have < tindex:
            added = safe_call(method(tl, "AddTrack"), "video")
            steps.append(f"AddTrack('video') -> {added!r}")
            if not added:
                raise RuntimeError(f"cannot add video track V{tindex}")
            have += 1
            rec.setdefault("added_tracks", []).append(f"V{have}")
        info: dict[str, Any] = {"mediaPoolItem": mpi, "trackIndex": tindex, "recordFrame": int(tl_start + title.start_frame), "mediaType": 1}
        if title.duration_frames < (h_end - h_start) - TOL_FRAMES:  # shorter than the template: trim (0-based frames of the clip)
            info.update(startFrame=0, endFrame=int(title.duration_frames - 1))
        res = safe_call(method(mp, "AppendToTimeline"), [info])
        steps.append(f"AppendToTimeline({', '.join(f'{k}={info[k]}' for k in info if k != 'mediaPoolItem')}) -> "
                     f"{len(res) if isinstance(res, list) else res!r} item(s)")
        appended = list(res) if isinstance(res, list) else []
        if not appended:
            raise RuntimeError("AppendToTimeline returned no timeline item")
        found = _find_item(dump_timeline(tl), "video", tindex, rec["helper_timeline"], title.start_frame,
                           title.start_frame + title.duration_frames)
        steps.append(f"read-back V{tindex} {title.start_frame}-{title.start_frame + title.duration_frames}: "
                     f"{'found' if found else 'NOT found'}")
        if not found:
            seen = [f"{it.get('name')} {it.get('start')}-{it.get('end')}" for t in dump_timeline(tl)["tracks"]
                    if t["kind"] == "video" and t["index"] == tindex for it in t["items"]]
            raise RuntimeError(f"no item named '{rec['helper_timeline']}' at V{tindex} {title.start_frame}-"
                               f"{title.start_frame + title.duration_frames} after AppendToTimeline (V{tindex} holds: "
                               f"{'; '.join(seen) or 'nothing'})")
        rec["applied"].append("track and range")
        rec.update(status="created", resolve_name=rec["helper_timeline"], record=[found["start"], found["end"]])
    except Exception as e:  # noqa: BLE001 - one title never aborts the run
        rec["reason"] = f"{type(e).__name__}: {e}"
        rec["rollback"] = _rollback_title(run, tl, mp, helper, appended)
        safe_call(method(project, "SetCurrentTimeline"), tl)
    return rec


def _rollback_title(run: FinishRun, tl: Any, mp: Any, helper: Any, appended: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kept": run.keep_on_failure}
    if run.keep_on_failure:
        out["left_in_place"] = {"timeline_items": len(appended), "helper_timeline": helper is not None}
        return out
    if appended:
        try:
            out["delete_clips"] = bool(method(tl, "DeleteClips")(appended, False))
        except Exception as e:  # noqa: BLE001
            out["delete_clips"] = f"{type(e).__name__}: {e}"
    if helper is not None and (not appended or out.get("delete_clips") is True):
        try:
            out["delete_helper_timeline"] = bool(method(mp, "DeleteTimelines")([helper]))
        except Exception as e:  # noqa: BLE001
            out["delete_helper_timeline"] = f"{type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------------------------------------- report
def _checks_table(checks: list[dict[str, Any]]) -> str:
    lines = ["| check | required | result | detail |", "|---|---|---|---|"]
    for c in checks:
        d = str(c.get("detail", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['check']} | {'yes' if c.get('required') else ''} | {c['status'].upper()} | {d[:240]} |")
    return "\n".join(lines)


def render_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = [f"# Resolve finish report", "",
                    f"Status: **{str(rep['status']).upper()}**  ({rep.get('summary', '')})", ""]
    rs = rep.get("resolve") or {}
    L += [f"- Resolve: {rs.get('product') or '?'} {rs.get('version') or ''}",
          f"- Project: `{(rep.get('project') or {}).get('name')}`   timeline: `{(rep.get('timeline') or {}).get('name')}`",
          f"- Workspace: `{rep.get('workspace')}`   FilmCraft project v{(rep.get('inputs') or {}).get('filmcraft_project_version')}",
          f"- Python {rep['python']['version']} ({rep['python']['bits']}-bit) on {rep['os']['system']} {rep['os']['release']}", ""]
    if rep.get("diagnosis"):
        L += ["## Diagnosis", ""] + [f"- {d}" for d in rep["diagnosis"]] + [""]
    if rep.get("phases"):
        L += ["## Steps", ""] + [f"- **{p['phase']}**: {p['status']}" + (f" - {p['detail']}" if p.get("detail") else "") for p in rep["phases"]] + [""]
    if rep.get("final_checks"):
        L += ["## Final read-back vs FilmCraft timeline spec", "", _checks_table(rep["final_checks"]), ""]
    if rep.get("repairs"):
        L += ["## Media repairs (ReplaceClip)", ""]
        for r in rep["repairs"]:
            L.append(f"- {r['track']}[{r['position']}] {r['reason']}: was `{(r.get('was') or {}).get('name')}` -> "
                     f"`{os.path.basename(str(r['expected_path']))}`: **{r.get('result')}**" + (f" ({r['detail']})" if r.get("detail") else ""))
        staged = sorted({r["staged_copy"] for r in rep["repairs"] if r.get("staged_copy") and r.get("result") == "online"})
        if staged:
            L += [f"Isolated copies ({len(staged)}): Resolve refused ReplaceClip on the original of these stills because they have numbered "
                  f"siblings in their folder (it reads them as an image sequence), so the clips reference single-file copies instead. "
                  f"The Resolve project now references copies under `resolve_handoff/stills/` for those clips: keep that folder with the project. "
                  f"The originals were not modified.", ""] + [f"- `{p}`" for p in staged] + [""]
        else:
            L.append("")
    if rep.get("markers"):
        L += ["## Markers", ""] + [f"- frame {m['frame']} '{m['name']}': {m.get('result')}" + (f" ({m['detail']})" if m.get("detail") else "")
                                  for m in rep["markers"]] + [""]
    titles = rep.get("titles") or []
    if titles:
        L += ["## Titles (EXPERIMENTAL)", ""]
        for t in titles:
            L.append(f"- '{t['text']}' on {t['track']} frames {t['start_frame']}-{t['start_frame'] + t['duration_frames']}: **{t['status'].upper()}**"
                     + (f" - {t.get('reason')}" if t.get("reason") else ""))
        failed = [t for t in titles if t["status"] != "created"]
        if failed:
            L += ["", "Recreate these titles manually in Resolve (Effects > Titles > Text+), on the track and range shown:"]
            L += [f"- '{t['text']}' {t['track']} {t['start_frame']}-{t['start_frame'] + t['duration_frames']}" for t in failed]
        if any(t["status"] == "created" for t in titles):
            L += ["", "Created titles live in helper timelines named `trip-vlog title N [...]` (open one to edit the text). "
                      "Size and position from the spec are not applied."]
        L.append("")
    L += ["## Resolve API calls", "", f"{len(rep['api_calls'])} call(s) recorded in `{REPORT_JSON}` (`api_calls`). Mutating calls:", ""]
    for c in rep["api_calls"]:
        if c.get("mutating"):
            tag = "proven" if c["call"] in PROVEN_CALLS else "UNPROVEN on real Resolve"
            L.append(f"- `{c['call']}` {json.dumps(c.get('args'), ensure_ascii=False)[:200]} -> "
                     f"{json.dumps(c.get('result', c.get('error')), ensure_ascii=False)[:120]} ({tag})")
    L += ["", f"Read-only calls by name: {json.dumps(rep.get('read_call_counts', {}))}", ""]
    if rep.get("notes"):
        L += ["## Notes", ""] + [f"- {n}" for n in rep["notes"]] + [""]
    return "\n".join(L) + "\n"


def _record_manifest(root: Path, status: str, summary: dict[str, Any], error: str | None, started: str, project: str | None,
                     timeline: str | None, resolve_info: dict[str, Any] | None) -> None:
    ws = Workspace(root)
    m = Manifest.load(ws.manifest_path)
    m.stages[STAGE] = {"status": status, "decision": "ran", "started_at": started, "finished_at": utc_now_iso(),
                       "summary": summary, "error": error}
    runs = list((m.get(STAGE) or {}).get("runs") or [])
    if project:
        runs.append({"project_name": project, "timeline_name": timeline, "status": status, "at": utc_now_iso(),
                     "resolve": resolve_info or {}})
    m.set(**{STAGE: {"runs": runs}})
    m.save()


def _recorded_names(root: Path) -> list[str]:
    m = Manifest.load(Workspace(root).manifest_path)
    return [r.get("project_name") for r in (m.get(STAGE) or {}).get("runs", []) if r.get("project_name")]


# ---------------------------------------------------------------------------------------------- main entry
@dataclass
class FinishResult:
    exit_code: int
    status: str
    report: dict[str, Any] | None = None
    api: ApiSession | None = None


def _connect() -> tuple[Any, dict[str, Any]]:
    return resolve_factory() if resolve_factory is not None else connect_resolve()


def run_finish(output: Path | str, *, project_name: str | None = None, dry_run: bool = False, keep_on_failure: bool = False,
               echo: Callable[[str], None] = print) -> FinishResult:
    try:
        inp = load_inputs(output)
    except FinishError as e:
        echo(f"error: {e}")
        return FinishResult(EXIT_USAGE, "preconditions_failed")
    started = utc_now_iso()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    session = ApiSession(allowed_media=media_keys(inp.manifest_paths), redactions=((str(inp.root), "<workspace>"),),
                         stills_root=inp.handoff / STILLS_DIR)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "tool": "trip-vlog resolve-finish", "tool_version": __version__,
        "started_at": started, "workspace": str(inp.root), "dry_run": dry_run, "python": python_info(),
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "inputs": {"timeline_xml": str(inp.xml_path), "timeline_xml_sha256": inp.xml_sha256,
                   "filmcraft_project_version": inp.run.get("version"), "spec_hash": inp.run.get("spec_hash"),
                   "media_dir": inp.media_dir, "media_manifest_files": len(inp.rows),
                   "plan_hash": inp.plan.get("plan_hash"), "titles_in_spec": len(inp.spec.titles),
                   "markers_in_plan": len(inp.plan_markers)},
        "notes": list(inp.warnings),
    }
    resolve, info = _connect()
    if resolve is None:
        diag = list(info.get("diagnosis") or ["Resolve scripting is unavailable"])
        return _unavailable(inp, report, diag, started, dry_run, echo)
    api = session.wrap(resolve, "resolve")
    product = safe_call(method(api, "GetProductName"))
    version = safe_call(method(api, "GetVersionString"))
    report["resolve"] = {"product": product, "version": version, "version_tuple": safe_call(method(api, "GetVersion")),
                         "discovery": {k: v for k, v in info.items() if k not in ("diagnosis",)}}
    if "studio" not in str(product or "").lower():
        return _unavailable(inp, report, [f"Connected to '{product}', but resolve-finish needs DaVinci Resolve Studio with "
                                          "external scripting set to Local (Preferences > System > General)."], started, dry_run, echo)
    pm = safe_call(method(api, "GetProjectManager"))
    if pm is None:
        return _unavailable(inp, report, ["Resolve returned no ProjectManager (is a login/project-manager window open?)"], started,
                            dry_run, echo)
    existing = [p for p in (safe_call(method(pm, "GetProjectListInCurrentFolder")) or []) if isinstance(p, str)]
    recorded = _recorded_names(inp.root)
    if project_name:
        if project_name in existing:
            echo(f"error: a Resolve project named '{project_name}' already exists; resolve-finish never reuses or modifies "
                 "an existing project. Pick another --project-name or omit it.")
            return FinishResult(EXIT_USAGE, "name_collision", None, session)
        name = project_name
    else:
        name, _ = next_project_name(inp.project_title, existing, recorded)
    echo(f"Resolve: {product} {version}   workspace: {inp.root}")
    if dry_run:
        echo("DRY RUN: no project is created and nothing in Resolve is changed. Planned steps:")
        for i, s in enumerate(planned_steps(inp, name), 1):
            echo(f"  {i}. {s}")
        for w in inp.warnings:
            echo(f"  warning: {w}")
        report.update(status="dry_run", planned_steps=planned_steps(inp, name), project={"name": name},
                      api_calls=session.log, mutating_calls=len(session.mutating_calls()))
        return FinishResult(EXIT_OK, "dry_run", report, session)
    return _execute(inp, report, session, api, pm, name, existing, stamp, keep_on_failure, started, echo, bool(project_name))


def _unavailable(inp: FinishInputs, report: dict[str, Any], diag: list[str], started: str, dry_run: bool,
                 echo: Callable[[str], None]) -> FinishResult:
    echo("Could not drive DaVinci Resolve Studio through its scripting API:")
    for d in diag:
        echo(f"  - {d}")
    if not any("Python" in d and "fusionscript" in d for d in diag):
        echo(f"  - {python_compat_hint()}")
    echo("Requirements: DaVinci Resolve Studio running, Preferences > System > General > External scripting using = Local "
         "(restart Resolve afterwards). Nothing was changed. The plain FCP7 XML import (resolve_handoff/timeline.xml) "
         "remains the manual path.")
    if dry_run:
        echo("DRY RUN: Resolve is not needed to print the plan. Planned steps:")
        for i, s in enumerate(planned_steps(inp, None), 1):
            echo(f"  {i}. {s}")
        return FinishResult(EXIT_OK, "dry_run", report)
    report.update(status="manual_required", diagnosis=diag + [python_compat_hint()], finished_at=utc_now_iso(),
                  summary="Resolve scripting unavailable; nothing was changed", api_calls=[], phases=[])
    _write_reports(inp, report)
    _record_manifest(inp.root, "failed", {"status": "manual_required"}, "Resolve scripting unavailable", report["started_at"],
                     None, None, None)
    echo(f"Report: {inp.handoff / REPORT_JSON}")
    return FinishResult(EXIT_UNAVAILABLE, "manual_required", report)


def _write_reports(inp: FinishInputs, report: dict[str, Any]) -> None:
    atomic_write_json(inp.handoff / REPORT_JSON, report)
    atomic_write_text(inp.handoff / REPORT_MD, render_markdown(report))


def _execute(inp: FinishInputs, report: dict[str, Any], session: ApiSession, api: Any, pm: Any, name: str, existing: list[str],
             stamp: str, keep_on_failure: bool, started: str, echo: Callable[[str], None], explicit_name: bool) -> FinishResult:
    run = FinishRun(inp, session, stamp, keep_on_failure, echo=echo)
    spec = inp.spec
    echo("WARNING: a new Resolve project is created, which closes the currently open project. Save your work first.")
    error: str | None = None
    tl_name = f"{spec.sequence_name} (trip-vlog {stamp})"
    report["project"] = {"name": name, "folder_projects_before": len(existing), "explicit_name": explicit_name}
    report["timeline"] = {"name": tl_name}
    final_checks: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    titles: list[dict[str, Any]] = []
    status = "fail"
    try:
        # 1. project
        project = None
        collisions: list[str] = []
        for _attempt in range(5):
            project = safe_call(method(pm, "CreateProject"), name)
            if project is not None:
                break
            collisions.append(name)
            if explicit_name:
                break
            name, _ = next_project_name(inp.project_title, [*existing, *collisions], _recorded_names(inp.root))
        report["project"].update(name=name, collisions_skipped=collisions)
        if project is None:
            run.phase("create project", "failed", f"CreateProject('{name}') returned None")
            raise RuntimeError("Resolve refused to create the project (name clash, or the open project could not be closed)")
        run.phase("create project", "ok", name)
        sets = {"timelineFrameRate": fps_setting(spec.fps), "timelineResolutionWidth": str(spec.width),
                "timelineResolutionHeight": str(spec.height)}
        bad = [k for k, v in sets.items() if not safe_call(method(project, "SetSetting"), k, v)]
        run.phase("project settings", "ok" if not bad else "warn",
                  ", ".join(f"{k}={v}" for k, v in sets.items()) + (f"; SetSetting refused: {', '.join(bad)}" if bad else ""))
        mp = safe_call(method(project, "GetMediaPool"))
        if mp is None:
            raise RuntimeError("GetMediaPool() returned None")

        # 2. import
        opts: dict[str, Any] = {"timelineName": tl_name, "importSourceClips": True}
        if inp.media_dir:
            opts["sourceClipsPath"] = native_path(inp.media_dir)
        tl = safe_call(method(mp, "ImportTimelineFromFile"), str(inp.xml_path.resolve()), opts)
        if tl is None:
            run.phase("import timeline", "failed", "ImportTimelineFromFile returned None; see Workspace > Console in Resolve")
            raise RuntimeError("Resolve could not import resolve_handoff/timeline.xml")
        safe_call(method(project, "SetCurrentTimeline"), tl)
        run.phase("import timeline", "ok", f"imported as '{tl_name}'")
        transitions = count_xml_transitions(inp.xml_path)
        plan_markers = inp.plan_markers

        def expected(title_names: dict[int, str] | None = None) -> dict[str, Any]:
            return expected_from_spec(spec, markers=plan_markers, transitions=transitions, title_names=title_names)

        actual = dump_timeline(tl)
        initial = compare_timeline(expected({}), actual)
        report["initial_checks"] = initial

        # 3. repairs
        repairs = repair_media(run, tl, expected({}), actual)
        if repairs:
            fixed = sum(1 for r in repairs if r.get("result") in ("online", "already_online"))
            run.phase("repair media", "ok" if fixed == len(repairs) else "warn", f"{fixed}/{len(repairs)} clip(s) repaired with ReplaceClip"
                      + (f" ({len(run.staged)} via isolated copies in {STILLS_DIR_REL})" if run.staged else ""))
            actual = dump_timeline(tl)
            if run.staged:
                run.notes.append(f"The Resolve project references isolated copies of {len(run.staged)} still(s) under {STILLS_DIR_REL}/ "
                                 "(Resolve refused the originals, which have numbered siblings); keep that folder with the project.")
                report["staged_copies"] = [{"copy": c, "source": o} for c, o in run.staged.items()]
        else:
            run.phase("repair media", "ok", "nothing offline or misread")

        # 4. markers
        markers = sync_markers(run, tl, plan_markers, actual)
        added = sum(1 for m in markers if m["result"] == "added")
        run.phase("markers", "ok" if all(m["result"] in ("present", "added") for m in markers) else "warn",
                  f"{len(markers)} expected, {added} added, {sum(1 for m in markers if m['result'] == 'present')} already present")
        ck = safe_call(method(pm, "SaveProject"))
        run.phase("save checkpoint", "ok" if ck else "warn", f"SaveProject returned {ck!r}")

        # 5. titles (experimental)
        tl_start = actual.get("start_frame") or 0
        for i, t in enumerate(spec.titles):
            rec = create_title(run, project, mp, tl, i, t, tl_start)
            titles.append(rec)
        if spec.titles:
            ok_n = sum(1 for t in titles if t["status"] == "created")
            run.phase("titles (experimental)", "ok" if ok_n == len(titles) else "warn",
                      f"{ok_n}/{len(titles)} created and verified" + ("" if ok_n == len(titles) else "; the rest are listed in the report for manual recreation"))

        # 6. final read-back
        final = dump_timeline(tl)
        names = {t["index"]: t["resolve_name"] for t in titles if t["status"] == "created"}
        final_checks = [_r("import", "pass", f"imported as '{final.get('name')}'")] + compare_timeline(
            expected(names), final, path_aliases=run.staged)
        if spec.titles:
            created = [t for t in titles if t["status"] == "created"]
            final_checks = [c for c in final_checks if c["check"] != "titles"] + [
                _r("titles", "pass" if len(created) == len(titles) else "fail",
                   f"{len(created)}/{len(titles)} created and verified (experimental)"
                   + "".join(f"; FAILED '{t['text']}': {t.get('reason')}" for t in titles if t["status"] != "created"), required=False)]
        saved = safe_call(method(pm, "SaveProject"))
        run.phase("save project", "ok" if saved else "warn", f"SaveProject returned {saved!r}")
        report["final_timeline"] = final
        req_ok = format_passed(final_checks)
        soft_ok = all(c["status"] in ("pass", "info") for c in final_checks)
        status = "pass" if req_ok and soft_ok else "partial" if req_ok else "fail"
    except Exception as e:  # noqa: BLE001 - the run must still write its report
        error = f"{type(e).__name__}: {e}"
        run.phase("run", "failed", error)
        status = "fail"
    failed_titles = [t for t in titles if t["status"] != "created"]
    summary = (f"{sum(1 for c in final_checks if c['status'] == 'pass')} checks passed, "
               f"{sum(1 for c in final_checks if c['status'] == 'fail')} failed; "
               f"{len(repairs)} media repair(s); {sum(1 for m in markers if m['result'] == 'added')} marker(s) added; "
               f"{len(titles) - len(failed_titles)}/{len(titles)} title(s) created")
    report.update(status=status, error=error, summary=summary, phases=run.phases, repairs=repairs, markers=markers,
                  titles=titles, final_checks=final_checks, finished_at=utc_now_iso(),
                  api_calls=session.log, dropped_api_calls=session.dropped_calls,
                  read_call_counts={k: v for k, v in session.counts().items() if not _is_mutating(k)},
                  mutating_calls=len(session.mutating_calls()), notes=report["notes"] + run.notes,
                  titles_need_manual_recreation=[{"text": t["text"], "track": t["track"], "start_frame": t["start_frame"],
                                                  "duration_frames": t["duration_frames"], "reason": t.get("reason")}
                                                 for t in failed_titles])
    _write_reports(inp, report)
    _record_manifest(inp.root, {"pass": "succeeded", "partial": "partial"}.get(status, "failed"),
                     {"status": status, "project": report["project"]["name"], "timeline": tl_name, "repairs": len(repairs),
                      "markers_added": sum(1 for m in markers if m["result"] == "added"), "titles_created": len(titles) - len(failed_titles),
                      "titles_failed": len(failed_titles), "report": f"resolve_handoff/{REPORT_JSON}"},
                     error, started, report["project"]["name"], tl_name, {k: report["resolve"].get(k) for k in ("product", "version")})
    echo("")
    echo(f"Result: {status.upper()}  ({summary})")
    for c in final_checks:
        if c["status"] == "fail":
            echo(f"  FAIL {c['check']}: {str(c['detail'])[:200]}")
    for t in failed_titles:
        echo(f"  title needs manual recreation: '{t['text']}' on {t['track']} frames {t['start_frame']}-"
             f"{t['start_frame'] + t['duration_frames']} ({t.get('reason')})")
    echo(f"Resolve project kept: '{report['project']['name']}'   report: {inp.handoff / REPORT_JSON}")
    return FinishResult(EXIT_FAILED if status == "fail" else EXIT_OK, status, report, session)


def _is_mutating(call: str) -> bool:
    return call.split(".", 1)[-1] in MUTATING
