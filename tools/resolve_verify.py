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
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import posixpath
import re
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path, PureWindowsPath
from typing import Any

TOL_FRAMES = 1
SCHEMA_VERSION = 1
REQUIRED_CHECKS = ("import", "fps", "duration", "clip_order", "record_inout", "source_in", "media_online")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".exr", ".dpx"}
FORMAT_BY_SUFFIX = {".otio": "otio", ".xml": "fcp7xml", ".fcpxml": "fcpxml", ".edl": "edl", ".aaf": "aaf"}
# Import order. Formats observed to crash/freeze/fail on Resolve 21.1 are "risky": skipped unless
# --include-risky-formats, and then run last (fcpxml absolutely last, it crashes Resolve).
SAFE_FORMATS = ("fcp7xml", "otio")
RISKY_ORDER = ("edl", "aaf", "fcpxml")
OTIO_VARIANTS = ("bare", "slash", "file_url")
SKIP_RISKY_MSG = ("skipped \u2014 observed to crash/freeze/fail on Resolve 21.1; pass --include-risky-formats to try")
SCRATCH_DEFAULT_NAMES = (None, "", "Untitled Project")


# --------------------------------------------------------------------------- discovery
def default_script_paths(plat: str | None = None, env: dict | None = None) -> tuple[str, str]:
    """Documented default (API dir, fusionscript library) for a platform (sys.platform style)."""
    plat = plat or sys.platform
    env = os.environ if env is None else env
    if plat.startswith("win"):
        pd = env.get("PROGRAMDATA", r"C:\ProgramData")
        return (pd + r"\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
                r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
    if plat == "darwin":
        return ("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
                "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
    return ("/opt/resolve/Developer/Scripting", "/opt/resolve/libs/Fusion/fusionscript.so")


def discover_resolve(plat: str | None = None, env: dict | None = None) -> dict:
    """Resolve RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB (env wins over defaults) and check existence."""
    env = os.environ if env is None else env
    d_api, d_lib = default_script_paths(plat, env)
    api = env.get("RESOLVE_SCRIPT_API") or d_api
    lib = env.get("RESOLVE_SCRIPT_LIB") or d_lib
    modules = os.path.join(api, "Modules")
    return {
        "api": api, "lib": lib, "modules": modules,
        "api_source": "env" if env.get("RESOLVE_SCRIPT_API") else "default",
        "lib_source": "env" if env.get("RESOLVE_SCRIPT_LIB") else "default",
        "modules_exists": os.path.isdir(modules),
        "lib_exists": os.path.isfile(lib),
    }


def connect_resolve(plat: str | None = None, env: dict | None = None) -> tuple[Any, dict]:
    """Return (resolve_or_None, info). info['diagnosis'] lists reasons when None."""
    info = discover_resolve(plat, env)
    diag: list[str] = []
    if not info["modules_exists"]:
        diag.append(f"Scripting modules folder not found: {info['modules']} "
                    "(is DaVinci Resolve installed? set RESOLVE_SCRIPT_API to the 'Scripting' folder).")
    if not info["lib_exists"]:
        diag.append(f"fusionscript library not found: {info['lib']} (set RESOLVE_SCRIPT_LIB to its full path).")
    # Resolve's own helper module reads these when it loads fusionscript.
    os.environ.setdefault("RESOLVE_SCRIPT_API", info["api"])
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", info["lib"])
    if info["modules_exists"] and info["modules"] not in sys.path:
        sys.path.insert(0, info["modules"])
    mod = None
    if info["modules_exists"]:
        try:
            import DaVinciResolveScript as mod  # type: ignore  # noqa: N813
        except Exception as e:  # ImportError, OSError loading the dll, ...
            diag.append(f"import DaVinciResolveScript failed: {type(e).__name__}: {e}")
    resolve = None
    if mod is not None:
        try:
            resolve = mod.scriptapp("Resolve")
        except Exception as e:
            diag.append(f"scriptapp('Resolve') raised {type(e).__name__}: {e}")
        if resolve is None and not any("scriptapp" in x for x in diag):
            diag.append("scriptapp('Resolve') returned None. Either Resolve is not running, or external scripting is "
                        "disabled: set Preferences > System > General > 'External scripting using' = Local "
                        "(the free edition of Resolve may not offer external scripting at all; Studio does).")
    info["diagnosis"] = diag
    return resolve, info


# --------------------------------------------------------------------------- paths
def normalize_path(p: Any, base_dir: str | os.PathLike | None = None, windows: bool | None = None) -> str:
    """Canonical comparison form: file:// URLs decoded, relative paths resolved against base_dir,
    forward slashes, no trailing slash. Case folded when windows (drive paths are case-insensitive;
    macOS volumes usually are too, so callers may also fold there via fold_case)."""
    if p is None:
        return ""
    s = str(p).strip()
    if not s:
        return ""
    is_url = s.lower().startswith("file:")
    if is_url:
        u = urllib.parse.urlparse(s)
        host = u.netloc
        path = urllib.parse.unquote(u.path)
        if host and host.lower() != "localhost":  # file://server/share/x -> //server/share/x (UNC)
            path = "//" + host + path
        # /C:/dir -> C:/dir
        if re.match(r"^/[A-Za-z]:[/\\]", path) or re.match(r"^/[A-Za-z]:$", path):
            path = path[1:]
        s = path
    s = s.replace("\\", "/")
    if s.startswith("\\\\?\\") or s.startswith("//?/"):
        s = s[4:]
    drive = re.match(r"^[A-Za-z]:/", s) is not None or s.startswith("//")
    if not drive and not s.startswith("/") and base_dir is not None:
        s = os.path.normpath(os.path.join(str(base_dir).replace("\\", "/"), s)).replace("\\", "/")
    else:
        # collapse ./ and ../ without touching UNC / drive roots
        prefix = ""
        m = re.match(r"^([A-Za-z]:)(/.*)?$", s)
        if m:
            prefix, rest = m.group(1), m.group(2) or "/"
        elif s.startswith("//"):
            prefix, rest = "//", s[2:]
        else:
            rest = s
        norm = os.path.normpath(rest.replace("/", os.sep)).replace("\\", "/") if rest else rest
        if prefix == "//" and not norm.startswith("/"):
            norm = norm
        s = prefix + norm if prefix != "//" else "//" + norm.lstrip("/")
    if len(s) > 1:
        s = s.rstrip("/")
    if windows is None:
        windows = os.name == "nt"
    return s.lower() if windows else s


def paths_equal(a: Any, b: Any, base_a: Any = None, base_b: Any = None, fold_case: bool | None = None) -> bool:
    if fold_case is None:
        fold_case = os.name == "nt" or sys.platform == "darwin"
    na, nb = normalize_path(a, base_a, windows=False), normalize_path(b, base_b, windows=False)
    if not na or not nb:
        return False
    return na.lower() == nb.lower() if fold_case else na == nb


# --------------------------------------------------------------------------- OTIO (stdlib reader)
def _rt(o: Any) -> tuple[float, float] | None:
    if isinstance(o, dict) and "value" in o and "rate" in o:
        return float(o["value"]), float(o["rate"])
    return None


def _frames(rt: tuple[float, float] | None, fps: float) -> int:
    if rt is None or rt[1] == 0:
        return 0
    return int(round(rt[0] * fps / rt[1]))


def _range(o: Any, fps: float) -> tuple[int, int] | None:
    if not isinstance(o, dict):
        return None
    st, du = _rt(o.get("start_time")), _rt(o.get("duration"))
    if st is None or du is None:
        return None
    return _frames(st, fps), _frames(du, fps)


def _media_ref(clip: dict) -> dict | None:
    refs = clip.get("media_references")
    if isinstance(refs, dict) and refs:
        key = clip.get("active_media_reference_key") or "DEFAULT_MEDIA"
        return refs.get(key) or next(iter(refs.values()))
    mr = clip.get("media_reference")
    return mr if isinstance(mr, dict) else None


def _schema(o: Any) -> str:
    return str(o.get("OTIO_SCHEMA", "")).split(".")[0] if isinstance(o, dict) else ""


def read_otio_expected(path: str | os.PathLike) -> dict:
    """Parse an .otio (JSON) file into the expected-timeline dict. No OTIO library needed."""
    p = Path(path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    # Resolve relative target_urls against the .otio file's ABSOLUTE directory (cwd-independent).
    return otio_to_expected(doc, base_dir=p.resolve().parent)


def otio_to_expected(doc: dict, base_dir: str | os.PathLike | None = None) -> dict:
    if _schema(doc) != "Timeline":
        raise ValueError("not an OTIO Timeline document")
    stack = doc.get("tracks") or {}
    tracks = [t for t in stack.get("children", []) if _schema(t) == "Track"]
    meta = (doc.get("metadata") or {}).get("filmcraft", {}).get("settings", {})
    # frame rate: filmcraft settings, else global_start_time, else first clip's rate
    fps = None
    fr = meta.get("frame_rate")
    if isinstance(fr, dict) and fr.get("num") and fr.get("den"):
        fps = fr["num"] / fr["den"]
    if fps is None:
        g = _rt(doc.get("global_start_time"))
        fps = g[1] if g else None
    if fps is None:
        for t in tracks:
            for c in t.get("children", []):
                r = _range(c.get("source_range"), 1) if _schema(c) == "Clip" else None
                rt = _rt((c.get("source_range") or {}).get("duration"))
                if rt:
                    fps = rt[1]
                    break
            if fps:
                break
    fps = float(fps or 24.0)

    out_tracks, transitions, total = [], 0, 0
    for t in tracks:
        kind = str(t.get("kind", "Video")).lower()
        pos, clips = 0, []
        for c in t.get("children", []):
            sch = _schema(c)
            if sch == "Transition":
                transitions += 1
                continue
            sr = _range(c.get("source_range"), fps)
            if sch == "Gap":
                pos += sr[1] if sr else 0
                continue
            if sch != "Clip" or sr is None:
                continue
            mr = _media_ref(c)
            src = None
            if mr and _schema(mr) == "ExternalReference" and mr.get("target_url"):
                src = normalize_path(mr["target_url"], base_dir, windows=False)
            clips.append({
                "name": c.get("name") or "", "source_path": src,
                "record_start_frame": pos, "record_end_frame": pos + sr[1],
                "source_in_frame": sr[0], "source_out_frame": sr[0] + sr[1],
            })
            pos += sr[1]
        total = max(total, pos)
        out_tracks.append({"name": t.get("name") or f"{kind} {len(out_tracks) + 1}", "kind": kind, "clips": clips})

    markers = []

    def collect(o: Any) -> None:
        for m in (o.get("markers") or []) if isinstance(o, dict) else []:
            mr = _range(m.get("marked_range"), fps)
            if mr:
                markers.append({"name": m.get("name") or "", "frame": mr[0]})

    collect(stack)
    for t in tracks:
        collect(t)
    gs = _frames(_rt(doc.get("global_start_time")), fps)
    if gs:
        for m in markers:
            m["frame"] -= gs
    return {
        "name": doc.get("name") or "", "fps": fps,
        "width": meta.get("width"), "height": meta.get("height"),
        "duration_frames": total, "transitions": transitions,
        "markers": markers, "tracks": out_tracks, "derived_from": "otio",
    }


def load_expected_json(path: str | os.PathLike) -> dict:
    exp = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("fps", "tracks"):
        if k not in exp:
            raise ValueError(f"expected timeline JSON lacks '{k}'")
    exp.setdefault("markers", [])
    exp.setdefault("transitions", None)
    base = Path(path).resolve().parent
    for t in exp["tracks"]:
        for c in t.get("clips", []):
            if c.get("source_path"):
                c["source_path"] = normalize_path(c["source_path"], base, windows=False)
    return exp


# --------------------------------------------------------------------------- Resolve dump
def _m(obj: Any, name: str):
    """getattr that tolerates API methods missing in this Resolve version."""
    def missing(*a, **k):
        raise AttributeError(f"{type(obj).__name__} has no method {name}")
    return getattr(obj, name, missing)


def _safe(fn, *a, default=None, errors: list | None = None, label: str = ""):
    """Call a Resolve API method defensively; every call may raise or return None."""
    try:
        return fn(*a)
    except Exception as e:
        if errors is not None:
            errors.append(f"{label or getattr(fn, '__name__', 'call')}: {type(e).__name__}: {e}")
        return default


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dump_timeline(timeline: Any) -> dict:
    """Read frame rate, resolution, duration, tracks, items and markers through documented calls."""
    errs: list[str] = []
    start = _num(_safe(_m(timeline, "GetStartFrame"), errors=errs, label="GetStartFrame")) or 0
    end = _num(_safe(_m(timeline, "GetEndFrame"), errors=errs, label="GetEndFrame"))
    out: dict[str, Any] = {
        "name": _safe(_m(timeline, "GetName"), errors=errs, label="GetName"),
        "start_frame": start, "end_frame": end,
        "duration_frames": (end - start) if end is not None else None,
        "fps": _num(_safe(_m(timeline, "GetSetting"), "timelineFrameRate", errors=errs, label="GetSetting(timelineFrameRate)")),
        "width": _num(_safe(_m(timeline, "GetSetting"), "timelineResolutionWidth", errors=errs, label="GetSetting(width)")),
        "height": _num(_safe(_m(timeline, "GetSetting"), "timelineResolutionHeight", errors=errs, label="GetSetting(height)")),
        "tracks": [], "markers": [], "errors": errs,
    }
    for kind in ("video", "audio"):
        n = int(_num(_safe(_m(timeline, "GetTrackCount"), kind, default=0, errors=errs, label=f"GetTrackCount({kind})")) or 0)
        for idx in range(1, n + 1):
            tname = _safe(_m(timeline, "GetTrackName"), kind, idx, errors=errs, label="GetTrackName") if True else None
            items = _safe(_m(timeline, "GetItemListInTrack"), kind, idx, default=[], errors=errs, label=f"GetItemListInTrack({kind},{idx})") or []
            rec = []
            for it in items:
                rec.append(_dump_item(it, start, errs))
            rec.sort(key=lambda r: (r["start"] if r["start"] is not None else 0))
            out["tracks"].append({"kind": kind, "index": idx, "name": tname or f"{kind} {idx}", "items": rec})
    mk = _safe(_m(timeline, "GetMarkers"), default={}, errors=errs, label="GetMarkers") or {}
    for fid, m in mk.items():
        f = _num(fid)
        if f is None:
            continue
        if start > 1000 and f >= start:  # defensive: some builds report absolute frames
            f -= start
        out["markers"].append({"frame": int(f), "name": (m or {}).get("name") or (m or {}).get("note") or "",
                               "color": (m or {}).get("color")})
    out["markers"].sort(key=lambda m: m["frame"])
    return out


def _dump_item(it: Any, tl_start: float, errs: list) -> dict:
    s = _num(_safe(_m(it, "GetStart"), errors=errs, label="Item.GetStart"))
    e = _num(_safe(_m(it, "GetEnd"), errors=errs, label="Item.GetEnd"))
    d = _num(_safe(_m(it, "GetDuration"), errors=errs, label="Item.GetDuration"))
    lo = _num(_safe(_m(it, "GetLeftOffset"), errors=errs, label="Item.GetLeftOffset"))
    mpi = _safe(_m(it, "GetMediaPoolItem"), errors=errs, label="Item.GetMediaPoolItem")
    path = None
    if mpi is not None:
        path = _safe(_m(mpi, "GetClipProperty"), "File Path", errors=errs, label="GetClipProperty(File Path)")
        if isinstance(path, dict):
            path = path.get("File Path")
    exists = None
    if path:
        try:
            exists = os.path.exists(path)
        except OSError:
            exists = None
    rs = (s - tl_start) if s is not None else None
    re_ = (e - tl_start) if e is not None else None
    if re_ is None and rs is not None and d is not None:
        re_ = rs + d
    return {
        "name": _safe(_m(it, "GetName"), errors=errs, label="Item.GetName"),
        "start": rs, "end": re_, "duration": d, "source_in": lo,
        "has_media_pool_item": mpi is not None, "media_path": path or None,
        "media_exists_on_disk": exists,
        "online": bool(mpi is not None and path and exists is not False),
    }


# --------------------------------------------------------------------------- comparison
def _r(name: str, status: str, detail: str = "", required: bool | None = None) -> dict:
    return {"check": name, "status": status, "detail": detail,
            "required": (name in REQUIRED_CHECKS) if required is None else required}


def _within(a: float | None, b: float | None, tol: int = TOL_FRAMES) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _item_key(name: Any, path: Any) -> set[str]:
    keys = set()
    if name:
        keys.add(str(name).lower())
    if path:
        keys.add(os.path.basename(str(path).replace("\\", "/")).lower())
    return keys


def compare_timeline(expected: dict, actual: dict, fold_case: bool | None = None) -> list[dict]:
    """Return list of check results (pass/fail/info) for one imported timeline."""
    res: list[dict] = []
    # fps
    ef, af = _num(expected.get("fps")), actual.get("fps")
    if ef is None or af is None:
        res.append(_r("fps", "fail", f"expected {ef}, Resolve reported {af}"))
    else:
        res.append(_r("fps", "pass" if abs(ef - af) < 0.01 else "fail", f"expected {ef:g}, got {af:g}"))
    # resolution
    ew, eh = expected.get("width"), expected.get("height")
    if ew and eh:
        ok = actual.get("width") == ew and actual.get("height") == eh
        res.append(_r("resolution", "pass" if ok else "fail",
                      f"expected {ew}x{eh}, got {actual.get('width')}x{actual.get('height')}"))
    else:
        res.append(_r("resolution", "info", f"no expected resolution; Resolve has {actual.get('width')}x{actual.get('height')}"))
    # duration
    ed, ad = expected.get("duration_frames"), actual.get("duration_frames")
    if ed is None:
        res.append(_r("duration", "info", f"no expected duration; Resolve has {ad}"))
    else:
        res.append(_r("duration", "pass" if _within(ed, ad) else "fail", f"expected {ed}, got {ad} (tolerance +/-{TOL_FRAMES})"))

    # tracks
    exp_by_kind: dict[str, list[dict]] = {"video": [], "audio": []}
    for t in expected.get("tracks", []):
        exp_by_kind.setdefault(t.get("kind", "video"), []).append(t)
    act_by_kind: dict[str, list[dict]] = {"video": [], "audio": []}
    for t in actual.get("tracks", []):
        act_by_kind.setdefault(t["kind"], []).append(t)
    struct = []
    for kind in ("video", "audio"):
        ne = sum(1 for t in exp_by_kind.get(kind, []) if t.get("clips"))
        na = sum(1 for t in act_by_kind.get(kind, []) if t["items"])
        struct.append((kind, ne, na))
    ok = all(ne == na for _, ne, na in struct)
    res.append(_r("track_structure", "pass" if ok else "fail",
                  "; ".join(f"{k}: expected {ne} populated track(s), got {na}" for k, ne, na in struct)))

    order_bad, io_bad, src_bad, path_bad, online_bad, info_titles = [], [], [], [], [], []
    n_req = 0
    for kind in ("video", "audio"):
        etracks = [t for t in exp_by_kind.get(kind, []) if t.get("clips")]
        atracks = [t for t in act_by_kind.get(kind, []) if t["items"]]
        for i, et in enumerate(etracks):
            at = atracks[i] if i < len(atracks) else None
            label = f"{kind[0].upper()}{i + 1}"
            optional = [c for c in et["clips"] if not c.get("source_path")]
            required = [c for c in et["clips"] if c.get("source_path")]
            items = list(at["items"]) if at else []
            opt_names = {str(c.get("name", "")).lower() for c in optional}
            rest = []
            for it in items:
                if it["name"] and str(it["name"]).lower() in opt_names and not it.get("media_path"):
                    continue
                rest.append(it)
            for c in optional:
                found = any(str(it["name"]).lower() == str(c.get("name", "")).lower() for it in items)
                info_titles.append(f"{label} '{c.get('name')}' (no media; titles/graphics): {'present' if found else 'absent'} in Resolve")
            if len(rest) != len(required):
                order_bad.append(f"{label}: expected {len(required)} clip(s), got {len(rest)}")
            for j, c in enumerate(required):
                n_req += 1
                if j >= len(rest):
                    order_bad.append(f"{label}[{j}] '{c.get('name')}' missing")
                    continue
                it = rest[j]
                if not (_item_key(c.get("name"), c.get("source_path")) & _item_key(it["name"], it["media_path"])):
                    order_bad.append(f"{label}[{j}] expected '{c.get('name')}', got '{it['name']}'")
                if not (_within(c.get("record_start_frame"), it["start"]) and _within(c.get("record_end_frame"), it["end"])):
                    io_bad.append(f"{label}[{j}] '{c.get('name')}' expected {c.get('record_start_frame')}-{c.get('record_end_frame')}, got {it['start']}-{it['end']}")
                ext = os.path.splitext(str(c.get("source_path")))[1].lower()
                if ext in IMAGE_EXTS:
                    pass  # stills have no meaningful source in-point
                elif not _within(c.get("source_in_frame"), it["source_in"]):
                    src_bad.append(f"{label}[{j}] '{c.get('name')}' expected {c.get('source_in_frame')}, got {it['source_in']}")
                if not it["media_path"]:
                    path_bad.append(f"{label}[{j}] '{c.get('name')}' Resolve reports no File Path")
                elif not paths_equal(c.get("source_path"), it["media_path"], fold_case=fold_case):
                    path_bad.append(f"{label}[{j}] expected {c.get('source_path')}, got {it['media_path']}")
                if not it["online"]:
                    online_bad.append(f"{label}[{j}] '{c.get('name')}' offline ({it['media_path'] or 'no media pool item/path'})")
    res.append(_r("clip_order", "fail" if order_bad else "pass", "; ".join(order_bad) or f"{n_req} media clip(s) in expected order"))
    res.append(_r("record_inout", "fail" if io_bad else "pass", "; ".join(io_bad) or f"record in/out within +/-{TOL_FRAMES} frame"))
    res.append(_r("source_in", "fail" if src_bad else "pass", "; ".join(src_bad) or f"source in within +/-{TOL_FRAMES} frame (stills skipped)"))
    res.append(_r("media_path", "fail" if path_bad else "pass", "; ".join(path_bad) or "media paths match"))
    res.append(_r("media_online", "fail" if online_bad else "pass", "; ".join(online_bad) or "all media online"))
    # markers
    em, am = expected.get("markers") or [], actual.get("markers") or []
    if not em and not am:
        res.append(_r("markers", "pass", "no markers expected or found"))
    else:
        miss = [m for m in em if not any(_within(m.get("frame"), a["frame"]) for a in am)]
        res.append(_r("markers", "pass" if not miss and len(em) == len(am) else "fail",
                      f"expected {len(em)}, got {len(am)}" + (f"; missing {[(m.get('name'), m.get('frame')) for m in miss]}" if miss else "")))
    et_ = expected.get("transitions")
    res.append(_r("transitions", "info",
                  f"expected {et_}; Resolve's scripting API exposes little about transitions, check visually on the Edit page"))
    res.append(_r("titles", "info", "; ".join(info_titles) or "no title/graphic clips expected; check visually"))
    if actual.get("errors"):
        res.append(_r("api_errors", "info", f"{len(actual['errors'])} API call(s) failed/None, first: {actual['errors'][0]}"))
    return res


def format_passed(checks: list[dict]) -> bool:
    return all(c["status"] == "pass" for c in checks if c["required"])


# --------------------------------------------------------------------------- OTIO path variants
def _is_abs_ref(raw: str) -> bool:
    n = normalize_path(raw, None, windows=False)
    return bool(re.match(r"^[A-Za-z]:/", n)) or n.startswith("/")


def format_media_ref(norm: str, style: str) -> str:
    """Render a normalized absolute path (forward slashes) as bare / slash / file_url."""
    win = bool(re.match(r"^[A-Za-z]:/", norm)) or norm.startswith("//")
    if style == "slash":
        return norm
    if style == "bare":
        return str(PureWindowsPath(norm)) if win else norm
    if style == "file_url":
        if norm.startswith("//"):
            return "file:" + urllib.parse.quote(norm, safe="/")
        if win:
            return "file:///" + urllib.parse.quote(norm, safe="/:")
        return "file://" + urllib.parse.quote(norm, safe="/")
    raise ValueError(f"unknown OTIO variant style: {style}")


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
    dirs = []
    for t in expected.get("tracks", []):
        for c in t.get("clips", []):
            sp = c.get("source_path")
            if sp:
                n = normalize_path(sp, None, windows=False)
                if _is_abs_ref(n):
                    dirs.append(posixpath.dirname(n))
    if not dirs:
        return None
    uniq = sorted(set(dirs))
    try:
        d = uniq[0] if len(uniq) == 1 else posixpath.commonpath(uniq)
    except ValueError:
        return None
    if not d or d in ("/", "."):
        return None
    return format_media_ref(d, "bare")


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
def _type_name(v: Any) -> str:
    return type(v).__name__


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


def run_import(resolve: Any, files: list[Path], expected: dict, keep_project: bool,
               scratch_prefix: str = "tripvlog_verify", include_risky: bool = False,
               work_dir: Path | None = None, stamp: str | None = None) -> dict:
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
                entry["checks"] = [_r("import", "fail", "ImportTimelineFromFile returned None (unsupported format in this Resolve version, "
                                      "parse error, or import dialog blocked); see Workspace > Console in Resolve")]
            else:
                _safe(_m(project, "SetCurrentTimeline"), tl)
                actual = dump_timeline(tl)
                entry["actual"] = actual
                entry["checks"] = [_r("import", "pass", f"imported as '{actual.get('name')}'"
                                      + ("" if len(attempts) == 1 else " (only the no-options retry worked)"))] + compare_timeline(expected, actual)
            entry["passed"] = format_passed(entry["checks"])
            result["formats"][label if label not in result["formats"] else f"{label}:{f.name}"] = entry
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
                     work_dir=out_path.parent / f"resolve_verify_work_{stamp}", stamp=stamp)
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
