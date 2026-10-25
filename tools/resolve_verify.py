#!/usr/bin/env python3
"""Verify that DaVinci Resolve imports a handoff timeline faithfully (DESIGN.md 15.4).

Run this on YOUR machine (Windows or macOS) with DaVinci Resolve open. Standard
library only, Python 3.10+. It uses Resolve's documented scripting API
(DaVinciResolveScript) to create a scratch project, import each interchange file,
dump the imported timeline and compare it with an expected timeline.

Examples
    python tools/resolve_verify.py --handoff path/to/resolve_handoff
    python tools/resolve_verify.py --spike-out spike/out   # fcp7 xml + otio (4 path variants) only
    python tools/resolve_verify.py --spike-out spike/out --include-risky-formats   # also EDL/AAF/FCPXML, last
    python tools/resolve_verify.py --timeline x.otio --timeline x.xml --expected expected_timeline.json

Exit codes: 0 = at least one format passed every required check; 1 = none did;
2 = usage/input error; 3 = Resolve scripting unavailable (manual checklist printed and
resolve_verification.json written with status "manual_required").

Expected-timeline JSON (frames are integers on the timeline, record_end is exclusive):
{
  "name": "Main", "fps": 30, "width": 1920, "height": 1080, "duration_frames": 195,
  "transitions": 2,
  "markers": [{"name": "Scene A", "frame": 60}],
  "tracks": [{"name": "Video 1", "kind": "video", "clips": [
      {"name": "landscape_5s.mp4", "source_path": "/abs/landscape_5s.mp4",
       "record_start_frame": 0, "record_end_frame": 105,
       "source_in_frame": 15, "source_out_frame": 120}]}]
}
A clip with "source_path": null (titles/graphics) is optional: reported as info only.
Transitions (items without a media pool item named Dissolve/Wipe/Dip/Fade/..., or short ones at a cut) are
excluded from the clip checks and counted for the (non-required) transitions check.
--experiments forces the FCP7 image-sequence experiments (they run automatically when a still is misdetected).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# The shared Resolve code lives in src/trip_vlog/resolve (also used by `trip-vlog resolve-finish`). Run from a repo
# checkout, this script uses the checkout's src/ folder, so `python tools/resolve_verify.py` works without installing
# the package; otherwise an installed trip_vlog is used.
_SRC = Path(__file__).resolve().parents[1] / "src"
if (_SRC / "trip_vlog" / "resolve").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trip_vlog.resolve.api import jsonable as _jsonable  # noqa: E402
from trip_vlog.resolve.api import method as _m  # noqa: E402
from trip_vlog.resolve.api import safe_call as _safe  # noqa: E402
from trip_vlog.resolve.api import type_name as _type_name  # noqa: E402
from trip_vlog.resolve.compare import REQUIRED_CHECKS, _r, compare_timeline, format_passed, match_sequence  # noqa: E402,F401
from trip_vlog.resolve.discovery import connect_resolve, default_script_paths, discover_resolve  # noqa: E402,F401
from trip_vlog.resolve.dump import IMAGE_EXTS, SEQ_RE, TOL_FRAMES, classify_items, dump_timeline, is_transition_name  # noqa: E402,F401
from trip_vlog.resolve.dump import dump_item as _dump_item  # noqa: E402
from trip_vlog.resolve.otio_read import load_expected_json, otio_to_expected, read_otio_expected  # noqa: E402,F401
from trip_vlog.resolve.otio_read import schema_of as _schema  # noqa: E402
from trip_vlog.resolve.paths import common_media_dir, format_media_ref, normalize_path, paths_equal  # noqa: E402,F401
from trip_vlog.resolve.paths import is_abs_ref as _is_abs_ref  # noqa: E402
from trip_vlog.resolve.paths import native_path as _native  # noqa: E402

SCHEMA_VERSION = 1
FORMAT_BY_SUFFIX = {".otio": "otio", ".xml": "fcp7xml", ".fcpxml": "fcpxml", ".edl": "edl", ".aaf": "aaf"}
# Import order. Formats observed to crash/freeze/fail on Resolve 21.1 are "risky": skipped unless
# --include-risky-formats, and then run last (fcpxml absolutely last, it crashes Resolve).
SAFE_FORMATS = ("fcp7xml", "otio")
RISKY_ORDER = ("edl", "aaf", "fcpxml")
OTIO_VARIANTS = ("bare", "slash", "file_url")
SKIP_RISKY_MSG = ("skipped \u2014 observed to crash/freeze/fail on Resolve 21.1; pass --include-risky-formats to try")
SCRATCH_DEFAULT_NAMES = (None, "", "Untitled Project")
OTIO_REJECTED_MSG = ("rejected by ImportTimelineFromFile in this Resolve version "
                     "(manual File > Import > Timeline may still work)")



def rewrite_otio_media(node: Any, base_dir: str | os.PathLike, style: str) -> int:
    """In place: rewrite ExternalReference.target_url values that are relative (or not an absolute
    path/URL) to absolute references in the given style. Everything else is untouched. Returns count."""
    n = 0
    if isinstance(node, dict):
        url = node.get("target_url")
        if _schema(node) == "ExternalReference" and isinstance(url, str) and url.strip() and not _is_abs_ref(url):
            node["target_url"] = format_media_ref(normalize_path(url, base_dir, windows=False), style)
            n += 1
        for v in node.values():
            n += rewrite_otio_media(v, base_dir, style)
    elif isinstance(node, list):
        for v in node:
            n += rewrite_otio_media(v, base_dir, style)
    return n


def make_otio_variants(otio_path: Path, work_dir: Path) -> tuple[dict[str, Path], list[str]]:
    """Write up to three absolute-media variants of an .otio under work_dir. Returns ({style: path}, notes)."""
    notes: list[str] = []
    out: dict[str, Path] = {}
    try:
        text = otio_path.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError) as e:
        return out, [f"OTIO variants skipped: cannot read {otio_path.name} as JSON ({e})"]
    base = otio_path.resolve().parent
    for style in OTIO_VARIANTS:
        doc = json.loads(text)
        n = rewrite_otio_media(doc, base, style)
        if n == 0:
            notes.append("OTIO variants skipped: no relative/unresolvable target_url to rewrite")
            return {}, notes
        d = work_dir / f"otio_{style}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / otio_path.name
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out[style] = p
        notes.append(f"otio[{style}]: rewrote {n} target_url value(s) -> {p}")
    return out, notes


def source_clips_dir(expected: dict) -> str | None:
    """Absolute directory holding the expected media (common parent if several), native separators."""
    return common_media_dir(c.get("source_path") for t in expected.get("tracks", []) for c in t.get("clips", []))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def build_import_plan(files: list[Path], include_risky: bool, work_dir: Path) -> tuple[list[dict], list[str]]:
    """Ordered list of import jobs: fcp7xml, otio (original + variants), then risky formats if enabled
    (edl, aaf, fcpxml last). Risky formats not enabled appear as skipped jobs."""
    jobs: list[dict] = []
    notes: list[str] = []
    for f in files:
        fmt = FORMAT_BY_SUFFIX.get(f.suffix.lower(), f.suffix.lower().lstrip("."))
        risky = fmt not in SAFE_FORMATS
        if fmt == "otio":
            jobs.append({"fmt": fmt, "label": "otio[original]", "file": f, "risky": False})
            variants, vnotes = make_otio_variants(f, work_dir)
            notes += vnotes
            for style in OTIO_VARIANTS:
                if style in variants:
                    jobs.append({"fmt": fmt, "label": f"otio[{style}]", "file": variants[style], "risky": False})
        else:
            jobs.append({"fmt": fmt, "label": fmt, "file": f, "risky": risky})

    def rank(j: dict) -> tuple[int, int]:
        if j["fmt"] in SAFE_FORMATS:
            return (0, SAFE_FORMATS.index(j["fmt"]))
        return (1, RISKY_ORDER.index(j["fmt"]) if j["fmt"] in RISKY_ORDER else -1)

    jobs.sort(key=rank)  # stable: otio variants keep original, bare, slash, file_url order
    for j in jobs:
        j["skip"] = bool(j["risky"] and not include_risky)
    return jobs, notes



# --------------------------------------------------------------------------- Resolve session
def import_with_retry(mp: Any, abs_path: str, options: dict) -> tuple[Any, list[dict]]:
    """Try ImportTimelineFromFile with options, then once more with the bare path. Records both attempts."""
    attempts: list[dict] = []
    tl = None
    for opts in (options, None):
        errs: list[str] = []
        args = (abs_path, opts) if opts is not None else (abs_path,)
        tl = _safe(_m(mp, "ImportTimelineFromFile"), *args, errors=errs, label="ImportTimelineFromFile")
        att: dict[str, Any] = {"options": opts, "result": None if tl is None else _type_name(tl)}
        if errs:
            att["errors"] = errs
        attempts.append(att)
        if tl is not None:
            break
    return tl, attempts



# --------------------------------------------------------------------------- still-sequence experiments
def _call(steps: list, obj: Any, name: str, *args: Any) -> Any:
    """Defensive API call that records the call, its arguments and its result in `steps`."""
    errs: list[str] = []
    r = _safe(_m(obj, name), *args, errors=errs, label=name)
    st: dict[str, Any] = {"call": name, "args": _jsonable(list(args)), "result": _jsonable(r)}
    if errs:
        st["errors"] = errs
    steps.append(st)
    return r


def _clip_names(res: Any) -> list:
    out = []
    for c in (res if isinstance(res, list) else []):
        n = _safe(_m(c, "GetName"))
        out.append(n if n is not None else _type_name(c))
    return out


def summarize_stills(dump: dict) -> list[dict]:
    """Every image-like clip of a dump: track, name, record range, path and online state."""
    out = []
    for t in dump.get("tracks", []):
        for it in t["items"]:
            if it.get("kind") == "transition":
                continue
            names = [str(it.get("name") or ""), os.path.basename(str(it.get("media_path") or "").replace("\\", "/"))]
            if any(os.path.splitext(re.sub(r"\[\d+-\d+\]", "", n))[1].lower() in IMAGE_EXTS for n in names if n):
                out.append({"track": f"{t['kind'][0].upper()}{t['index']}", "name": it.get("name"), "start": it.get("start"),
                            "end": it.get("end"), "media_path": it.get("media_path"), "online": it.get("online"),
                            "sequence_name": any(SEQ_RE.match(n) for n in names if n)})
    return out


def _item_state(item: Any, tl_start: float) -> dict:
    d = _dump_item(item, tl_start, [])
    return {k: d[k] for k in ("name", "start", "end", "media_path", "media_exists_on_disk", "online")}


def _import_experiment(label: str, desc: str, mp: Any, xml_path: str, flag: bool, media_dir: str | None,
                       pre: list[tuple[str, Any]], expected: dict, stamp: str, fold_case: bool | None) -> dict:
    """One extra FCP7 import as its own timeline. Never raises."""
    rec: dict[str, Any] = {"approach": label, "description": desc, "steps": [], "works": False}
    try:
        for call, arg in pre:
            r = _call(rec["steps"], mp, call, arg)
            rec["steps"][-1]["clip_names"] = _clip_names(r)
        opts: dict[str, Any] = {"timelineName": f"verify_{stamp}_exp_{_slug(label)}", "importSourceClips": flag}
        tl = _call(rec["steps"], mp, "ImportTimelineFromFile", xml_path, opts)
        if tl is None:
            rec["result"] = "ImportTimelineFromFile returned None"
            return rec
        dump = dump_timeline(tl)
        cs = {c["check"]: c for c in compare_timeline(expected, dump, fold_case)}
        rec["stills"] = summarize_stills(dump)
        rec["checks"] = {k: cs[k]["status"] for k in ("clip_order", "record_inout", "media_online", "image_sequence_misdetected") if k in cs}
        rec["works"] = bool(rec["stills"]) and all(v == "pass" for v in rec["checks"].values())
        rec["result"] = "still online with correct name and record range" if rec["works"] else "still not fixed: " + ", ".join(
            f"{k}={v}" for k, v in rec["checks"].items() if v != "pass")
    except Exception as e:  # an experiment must never abort the run
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["result"] = rec["error"]
    return rec


def _repair_experiment(tl: Any, mp: Any, tl_start: float, misdetections: list[dict], media_dir: str | None) -> dict:
    """Repair the misdetected item(s) of the ORIGINAL import in place (scratch project only): ReplaceClip with the
    expected absolute path, then MediaPool.RelinkClips as a separate attempt if that fails."""
    rec: dict[str, Any] = {"approach": "repair_replace_clip", "description": "MediaPoolItem.ReplaceClip(expected path), then MediaPool.RelinkClips",
                           "items": [], "steps": [], "works": False}
    try:
        if not misdetections:
            rec["result"] = "skipped: no misdetected item in the original import"
            return rec
        oks = []
        for m in misdetections:
            def live() -> Any:
                items = _safe(_m(tl, "GetItemListInTrack"), m["track_kind"], m["track_index"]) or []
                idx = m.get("item_index")
                return items[idx] if isinstance(idx, int) and 0 <= idx < len(items) else None
            it: dict[str, Any] = {"expected": m["expected_path"], "track": m["track"], "attempts": []}
            item = live()
            it["before"] = _item_state(item, tl_start) if item is not None else None
            mpi = _safe(_m(item, "GetMediaPoolItem")) if item is not None else None
            target = _native(m["expected_path"])
            r = _call(it["attempts"], mpi, "ReplaceClip", target) if mpi is not None else None
            if mpi is None:
                it["attempts"].append({"call": "ReplaceClip", "result": "skipped: item has no MediaPoolItem"})
            if not r:
                folder = media_dir or os.path.dirname(target)
                _call(it["attempts"], mp, "RelinkClips", [mpi] if mpi is not None else [], folder)
            item = live()
            it["after"] = _item_state(item, tl_start) if item is not None else None
            a = it["after"] or {}
            it["works"] = bool(a.get("online")) and not SEQ_RE.match(str(a.get("name") or "")) \
                and not SEQ_RE.match(os.path.basename(str(a.get("media_path") or "")))
            oks.append(it["works"])
            rec["items"].append(it)
        rec["works"] = bool(oks) and all(oks)
        rec["result"] = "item online with the expected file after repair" if rec["works"] else "repair did not bring the still online"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["result"] = rec["error"]
    return rec


def run_still_sequence_experiments(project: Any, mp: Any, tl: Any, actual: dict, xml_path: str, expected: dict,
                                   misdetections: list[dict], media_dir: str | None, stamp: str,
                                   fold_case: bool | None = None, forced: bool = False) -> dict:
    """Find what avoids or repairs the image-sequence misdetection (FCP7 XML only, scratch project only,
    nothing deleted, originals untouched). Every approach is wrapped; failures are recorded, never raised."""
    files = []
    for t in expected.get("tracks", []):
        for c in t.get("clips", []):
            sp = c.get("source_path")
            if sp and _native(sp) not in files:
                files.append(_native(sp))
    out: dict[str, Any] = {"triggered_by": "--experiments" if forced and not misdetections else "image_sequence_misdetected",
                           "misdetected": [{k: m[k] for k in ("track", "expected_file", "resolve_name", "resolve_path")} for m in misdetections],
                           "media_files": files, "approaches": []}
    if tl is None:
        out["note"] = "original import returned no timeline; nothing to experiment on"
        return out
    ap = out["approaches"]
    ap.append(_import_experiment("import_source_clips_no_path", "(i) importSourceClips=True WITHOUT sourceClipsPath",
                                 mp, xml_path, True, None, [], expected, stamp, fold_case))
    ap.append(_import_experiment("pre_import_media_then_xml", "(ii) ImportMedia([path]) per file, then importSourceClips=False",
                                 mp, xml_path, False, None, [("ImportMedia", [f]) for f in files], expected, stamp, fold_case))
    ap.append(_import_experiment("pre_import_media_dict_then_xml", "(iii) ImportMedia([{'FilePath': path}]) per file, then importSourceClips=False",
                                 mp, xml_path, False, None, [("ImportMedia", [{"FilePath": f}]) for f in files], expected, stamp, fold_case))
    ap.append(_repair_experiment(tl, mp, actual.get("start_frame") or 0, misdetections, media_dir))
    ok = [a for a in ap if a["works"]]
    out["recommended"] = ok[0]["approach"] if ok else None
    out["summary"] = [f"{a['approach']}: {'WORKS' if a['works'] else 'no'} - {a.get('result')}" for a in ap]
    return out


def run_import(resolve: Any, files: list[Path], expected: dict, keep_project: bool,
               scratch_prefix: str = "tripvlog_verify", include_risky: bool = False,
               work_dir: Path | None = None, stamp: str | None = None, experiments: bool = False) -> dict:
    """Create a scratch project, import each file, dump + compare. Returns the report fragment."""
    pm = _safe(_m(resolve, "GetProjectManager"))
    result: dict[str, Any] = {"formats": {}, "scratch_project": None, "scratch_kept": keep_project, "notes": []}
    if pm is None:
        result["notes"].append("GetProjectManager() returned None")
        return result
    stamp = stamp or f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="resolve_verify_"))
    work_dir = Path(work_dir).resolve()
    plan, pnotes = build_import_plan([Path(f) for f in files], include_risky, work_dir)
    result["notes"] += pnotes
    result["import_order"] = [j["label"] + (" (skipped)" if j["skip"] else "") for j in plan]
    media_dir = source_clips_dir(expected)
    result["source_clips_path"] = media_dir
    prev = _safe(_m(pm, "GetCurrentProject"))
    prev_name = _safe(_m(prev, "GetName")) if prev is not None else None
    folder = _safe(_m(pm, "GetCurrentFolder"))
    name = f"{scratch_prefix}_{stamp}"
    project = _safe(_m(pm, "CreateProject"), name)
    if project is None:
        result["notes"].append(f"CreateProject('{name}') failed (name clash, or a project is open with unsaved state)")
        return result
    result["scratch_project"] = name
    result["creation_folder"] = folder
    try:
        fps = expected.get("fps")
        sets = {"timelineFrameRate": f"{float(fps):g}" if fps else None,
                "timelineResolutionWidth": str(expected["width"]) if expected.get("width") else None,
                "timelineResolutionHeight": str(expected["height"]) if expected.get("height") else None}
        for k, v in sets.items():
            if v is not None:
                ok = _safe(_m(project, "SetSetting"), k, v)
                if not ok:
                    result["notes"].append(f"SetSetting({k},{v}) returned {ok!r}")
        mp = _safe(_m(project, "GetMediaPool"))
        for job in plan:
            f: Path = job["file"]
            fmt, label = job["fmt"], job["label"]
            abs_path = str(f.resolve())  # Resolve runs in another process/cwd: always absolute, native separators
            entry: dict[str, Any] = {"file": abs_path, "format": fmt}
            if job["skip"]:
                entry["skipped"] = True
                entry["actual"] = None
                entry["checks"] = [_r("import", "info", SKIP_RISKY_MSG, required=False)]
                entry["passed"] = False
                result["formats"][label if label not in result["formats"] else f"{label}:{f.name}"] = entry
                continue
            exists = os.path.exists(abs_path)
            entry["diagnostics"] = {"path_passed": abs_path, "exists": exists,
                                    "size_bytes": os.path.getsize(abs_path) if exists else None}
            tl_name = f"verify_{stamp}_{_slug(label)}"
            opts: dict[str, Any] = {"timelineName": tl_name, "importSourceClips": True}
            if media_dir:
                opts["sourceClipsPath"] = media_dir
            tl, attempts = (None, []) if mp is None else import_with_retry(mp, abs_path, opts)
            entry["attempts"] = attempts
            entry["diagnostics"]["resolve_return_type"] = _type_name(tl)
            if tl is None:
                entry["actual"] = None
                detail = (OTIO_REJECTED_MSG if fmt == "otio" else
                          "ImportTimelineFromFile returned None (unsupported format in this Resolve version, "
                          "parse error, or import dialog blocked); see Workspace > Console in Resolve")
                entry["checks"] = [_r("import", "fail", detail)]
            else:
                _safe(_m(project, "SetCurrentTimeline"), tl)
                actual = dump_timeline(tl)
                entry["actual"] = actual
                entry["checks"] = [_r("import", "pass", f"imported as '{actual.get('name')}'"
                                      + ("" if len(attempts) == 1 else " (only the no-options retry worked)"))] + compare_timeline(expected, actual)
                mis = next((c.get("misdetections") for c in entry["checks"] if c["check"] == "image_sequence_misdetected"), None) or []
                if fmt == "fcp7xml" and (mis or experiments) and "still_sequence" not in result.setdefault("experiments", {}):
                    result["experiments"]["still_sequence"] = run_still_sequence_experiments(
                        project, mp, tl, actual, abs_path, expected, mis, media_dir, stamp, forced=experiments)
            entry["passed"] = format_passed(entry["checks"])
            result["formats"][label if label not in result["formats"] else f"{label}:{f.name}"] = entry
        if any(e.get("format") == "otio" and e.get("actual") is None and not e.get("skipped") for e in result["formats"].values()):
            bare = result["formats"].get("otio[bare]", {}).get("file")
            result["otio_manual_suggestion"] = (
                f"OTIO: in Resolve use File > Import > Timeline and pick {bare or 'the .otio file (see the work folder)'} "
                "manually, then tell us whether the media links.")
    finally:
        if not keep_project:
            _cleanup(pm, project, name, prev_name, folder, result)
        elif prev_name:
            result["notes"].append(f"scratch project '{name}' kept; your previous project '{prev_name}' was not reopened")
    return result


def _cleanup(pm: Any, project: Any, name: str, prev_name: str | None, folder: Any, result: dict) -> None:
    """Delete ONLY the scratch project created by this run (`name`). Resolve cannot delete the loaded
    project, and DeleteProject acts on the Project Manager's current folder, so: close, reload the user's
    previous project, return to the creation folder, then delete. Every step is recorded."""
    steps: list[dict] = result.setdefault("cleanup_steps", [])

    def step(label: str, fn_name: str, *a) -> Any:
        errs: list[str] = []
        r = _safe(_m(pm, fn_name), *a, errors=errs, label=fn_name)
        steps.append({"step": label, "call": f"{fn_name}({', '.join(repr(x) if not hasattr(x, 'GetName') else '<project>' for x in a)})",
                      "result": r if isinstance(r, (bool, int, float, str, type(None))) else _type_name(r),
                      **({"errors": errs} if errs else {})})
        return r

    step("close scratch project", "CloseProject", project)
    loaded_prev = False
    if prev_name not in SCRATCH_DEFAULT_NAMES and prev_name != name:
        loaded_prev = bool(step("reload previous project", "LoadProject", prev_name))
    else:
        steps.append({"step": "reload previous project", "call": None, "result": "skipped (no named previous project)"})

    def go_to_folder() -> None:
        if not folder:
            steps.append({"step": "return to creation folder", "call": None, "result": "skipped (folder unknown)"})
            return
        cur = step("read current folder", "GetCurrentFolder")
        if cur == folder:
            steps.append({"step": "return to creation folder", "call": None, "result": "already there"})
            return
        if not step("open creation folder", "OpenFolder", folder):
            step("go to root folder", "GotoRootFolder")
            step("open creation folder (from root)", "OpenFolder", folder)

    go_to_folder()
    deleted = step("delete scratch project", "DeleteProject", name)
    if not deleted and not loaded_prev:
        # The scratch project may still count as current. Load some OTHER existing project (never the
        # scratch one), then retry the delete once.
        listed = step("list projects in folder", "GetProjectListInCurrentFolder")
        others = [p for p in (listed if isinstance(listed, list) else []) if isinstance(p, str) and p != name]
        if others:
            step("load another project so the scratch one is not current", "LoadProject", others[0])
            go_to_folder()
            deleted = step("delete scratch project (retry)", "DeleteProject", name)
    result["scratch_deleted"] = bool(deleted)
    if not deleted:
        result["notes"].append(
            f"could not delete scratch project '{name}'. Delete it manually: in Resolve open the Project Manager "
            f"(Home icon), make sure another project is open, find '{name}' in the folder it was created in "
            f"({folder or 'the current folder'}), right-click > Delete. Nothing else was deleted.")


# --------------------------------------------------------------------------- manual fallback
MANUAL_CHECKLIST = """\
Manual Resolve import checklist
  1. Create a project; Project Settings: timeline frame rate and resolution = expected values, BEFORE importing.
  2. File > Import > Timeline > Import AAF, EDL, XML... and pick each file (.otio if your Resolve offers it, else the .xml).
     Leave 'automatically import source clips' on and 'set to timeline frame rate/resolution' on.
  3. Per format record: imports without error? timeline length and fps; clip order and in/out vs expected;
     media online or needs relinking; V/A track structure; transitions present and length; titles (expect offline/empty);
     marker positions and names; audio sync of the first seconds against preview.mp4.
  4. Screenshot the Edit page and the import log per format; note Resolve version/edition (Help > About).
  5. Pass = at least one format has correct order, in/out, duration, fps and online media.
"""


def os_info() -> dict:
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "python": platform.python_version()}


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def markdown_table(formats: dict) -> str:
    if not formats:
        return ""
    names = list(formats)
    checks: list[str] = []
    for f in formats.values():
        for c in f["checks"]:
            if c["check"] not in checks:
                checks.append(c["check"])
    lines = ["| check | req | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for ch in checks:
        row, req = [], False
        for n in names:
            c = next((x for x in formats[n]["checks"] if x["check"] == ch), None)
            if c is None:
                row.append("-")
                continue
            req = req or c["required"]
            row.append(c["status"].upper() + ("" if c["status"] == "pass" else f": {c['detail'][:90]}"))
        lines.append(f"| {ch} | {'yes' if req else ''} | " + " | ".join(row) + " |")
    lines.append("| **overall** | | " + " | ".join("PASS" if formats[n]["passed"] else "FAIL" for n in names) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- inputs
def gather_inputs(args: argparse.Namespace) -> tuple[list[Path], dict | None, str]:
    """Return (timeline files, expected dict or None, description of expected source)."""
    files: list[Path] = [Path(t) for t in (args.timeline or [])]
    expected_path = Path(args.expected) if args.expected else None
    search: list[Path] = []
    if args.handoff:
        search.append(Path(args.handoff))
    if args.spike_out:
        search.append(Path(args.spike_out) / "interchange")
    for d in search:
        if not d.is_dir():
            raise FileNotFoundError(f"not a directory: {d}")
        found = sorted(p for p in d.iterdir() if p.suffix.lower() in FORMAT_BY_SUFFIX and p.is_file())
        if args.spike_out and d.name == "interchange":
            found = [p for p in found if p.stem == "phase0"]
        files += found
        if expected_path is None and (d / "expected_timeline.json").is_file():
            expected_path = d / "expected_timeline.json"
    files = [f for f in files if f.suffix.lower() in FORMAT_BY_SUFFIX or f.is_file()]
    for f in files:
        if not f.is_file():
            raise FileNotFoundError(f"timeline file not found: {f}")
    # de-dup preserve order; skip formats Resolve cannot import via this API
    seen, uniq = set(), []
    for f in files:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            uniq.append(f)
    expected, src = None, ""
    if expected_path is not None:
        expected, src = load_expected_json(expected_path), str(expected_path)
    else:
        otio = next((f for f in uniq if f.suffix.lower() == ".otio"), None)
        if otio is not None:
            expected, src = read_otio_expected(otio), f"derived from {otio} (stdlib OTIO reader)"
    return uniq, expected, src


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Verify a handoff timeline imports faithfully into DaVinci Resolve.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--handoff", help="a resolve_handoff/ folder (*.otio, *.xml, ..., optional expected_timeline.json)")
    ap.add_argument("--spike-out", help="Phase 0 spike output dir (uses <dir>/interchange/phase0.*)")
    ap.add_argument("--timeline", action="append", help="timeline file to import (repeatable)")
    ap.add_argument("--expected", help="expected_timeline.json")
    ap.add_argument("--output", help="where to write resolve_verification.json (default: the handoff dir, else cwd)")
    ap.add_argument("--keep-project", action="store_true", help="do not delete the scratch Resolve project afterwards")
    ap.add_argument("--experiments", action="store_true",
                    help="force the image-sequence experiments for the FCP7 XML even if no still was misdetected "
                         "(they run automatically when one is)")
    ap.add_argument("--include-risky-formats", action="store_true",
                    help="also try FCPXML/EDL/AAF (observed to crash/freeze/fail on Resolve 21.1); run last. Save your work first")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.handoff or args.spike_out or args.timeline):
        print("error: give --handoff, --spike-out or --timeline", file=sys.stderr)
        return 2
    try:
        files, expected, exp_src = gather_inputs(args)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not files:
        print("error: no timeline files (.otio/.xml/.fcpxml/.edl/.aaf) found", file=sys.stderr)
        return 2
    if expected is None:
        print("error: no expected timeline. Provide --expected FILE (see --help), put expected_timeline.json in the "
              "handoff dir, or include an .otio file to derive it from.", file=sys.stderr)
        return 2
    out_path = Path(args.output) if args.output else (Path(args.handoff) if args.handoff else Path.cwd()) / "resolve_verification.json"
    if out_path.is_dir():
        out_path = out_path / "resolve_verification.json"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "tool": "tools/resolve_verify.py", "os": os_info(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "inputs": [str(f) for f in files], "expected_source": exp_src, "expected": expected,
    }
    resolve, info = connect_resolve()
    report["discovery"] = {k: v for k, v in info.items()}
    if resolve is None:
        report["status"] = "manual_required"
        report["diagnosis"] = info["diagnosis"]
        write_report(out_path, report)
        print("Could not drive DaVinci Resolve through its scripting API:")
        for d in info["diagnosis"]:
            print(f"  - {d}")
        print()
        print(MANUAL_CHECKLIST)
        print("Files to import:")
        for f in files:
            print(f"  {f}")
        print(f"\nStatus written to {out_path} (status: manual_required)")
        return 3

    report["resolve"] = {
        "product": _safe(_m(resolve, "GetProductName")), "version": _safe(_m(resolve, "GetVersionString")),
        "version_tuple": _safe(_m(resolve, "GetVersion")),
    }
    print(f"Resolve: {report['resolve']['product']} {report['resolve']['version']}")
    print("WARNING: a scratch project is created, which closes the currently open project. Save your work first.")
    if args.include_risky_formats:
        print("WARNING: --include-risky-formats: FCPXML/EDL/AAF are observed to crash or freeze Resolve 21.1. "
              "They run LAST (FCPXML very last). SAVE YOUR WORK NOW; the report is written only if Resolve survives.")
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    res = run_import(resolve, files, expected, args.keep_project, include_risky=args.include_risky_formats,
                     experiments=args.experiments, work_dir=out_path.parent / f"resolve_verify_work_{stamp}", stamp=stamp)
    report.update(res)
    report["include_risky_formats"] = bool(args.include_risky_formats)
    ok = any(f["passed"] for f in res["formats"].values())
    report["status"] = "pass" if ok else "fail"
    report["required_checks"] = list(REQUIRED_CHECKS)
    write_report(out_path, report)
    print()
    print(markdown_table(res["formats"]))
    print()
    for label, e in res["formats"].items():
        d = e.get("diagnostics")
        if d:
            tries = ", ".join("opts=" + ("yes" if a["options"] else "none") + f"->{a['result']}" for a in e.get("attempts", []))
            print(f"[{label}] path={d['path_passed']} exists={d['exists']} size={d['size_bytes']} "
                  f"returned={d['resolve_return_type']} attempts: {tries}")
        elif e.get("skipped"):
            print(f"[{label}] info: {SKIP_RISKY_MSG}")
    for n in res["notes"]:
        print(f"note: {n}")
    if res.get("otio_manual_suggestion"):
        print(res["otio_manual_suggestion"])
    ex = res.get("experiments", {}).get("still_sequence")
    if ex:
        print("\nStill image-sequence experiments (FCP7 XML, scratch project):")
        for m in ex.get("misdetected", []):
            print(f"  misdetected: expected '{m['expected_file']}', Resolve read '{m['resolve_name']}'")
        for line in ex.get("summary", []):
            print(f"  {line}")
        print(f"  => " + (f"'{ex['recommended']}' gives an online still with the correct record range"
                          if ex.get("recommended") else "no approach gave an online still with the correct record range"))
        offline = any(c["check"] == "media_online" and c["status"] == "fail" for e in res["formats"].values() for c in e.get("checks", []))
        if offline and ex.get("recommended"):
            print(f"  media_online fails in the original import; use '{ex['recommended']}' to fix it.")
    imports_ok = [e for e in res["formats"].values() if not e.get("skipped")]
    if imports_ok and all(e["actual"] is None for e in imports_ok):
        print("\nEvery import returned None. Open Resolve's Console (Workspace > Console) right after running; it often shows "
              "the import error. Please copy what it prints and send it back with resolve_verification.json.")
    else:
        print("\nTip: Resolve's Console (Workspace > Console) may show import errors; copy them if anything failed.")
    print(f"\nResult: {report['status'].upper()}  (report: {out_path})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

