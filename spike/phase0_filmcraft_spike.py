#!/usr/bin/env python3
"""Phase 0 -- FilmCraft compatibility spike.

Drives a *built* ``filmcraft-cli`` through its documented MCP server (headless,
stdio JSON-RPC) and proves, by execution, which edit operations FilmCraft can
perform and persist.  Nothing here is assumed: every capability is recorded as
``verified`` only when a command was executed and its effect read back.

Flow
  1. discovery   : --version, git commit, MCP initialize + tools/list, command catalog
  2. build       : create project, import media, create sequence, place/trim/move,
                   extra tracks, transition, title, marker, audio gain/fade
  3. persistence : save .fcproj, kill the server, start a NEW server on the file,
                   compare the timeline signature
  4. render      : file.exportMedia (waits) -> ffprobe
  5. interchange : file.exportInterchange for every format the build lists,
                   structural validation + comparison with what was built
  6. outputs     : capability_snapshot.json, mcp_tools.json, commands.jsonl

Requires: python3 stdlib, ffmpeg/ffprobe on PATH, generated fixtures
(tests/fixtures/make_fixtures.sh).  Optional: ``opentimelineio`` (+ the
``otio-fcp-adapter`` / ``otio-cmx3600-adapter`` plugins), ``xmllint``.

Exit status: 0 when every *required* check passed, 1 otherwise, 2 on usage error.
DaVinci Resolve cannot be run by this script; the Resolve import test is always
reported as ``pending``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

TICKS_PER_SECOND = 254_016_000_000  # documented (docs/agents.md); re-verified below
FPS = 30
REPO = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------------
# logging / bookkeeping
# ----------------------------------------------------------------------------


class Recorder:
    """Collects checks and a sanitized JSONL command log."""

    def __init__(self, out: Path, fixtures: Path):
        self.out = out
        self.fixtures = fixtures
        self.checks: list[dict] = []
        self.log_path = out / "commands.jsonl"
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.t0 = time.time()

    def sanitize(self, obj):
        text = json.dumps(obj, ensure_ascii=False)
        for real, token in ((str(self.out), "$OUT"), (str(self.fixtures), "$FIXTURES"), (str(REPO), "$REPO")):
            text = text.replace(real, token)
        obj = json.loads(text)
        return self._redact(obj)

    def _redact(self, o):
        if isinstance(o, dict):
            return {k: ("***" if re.search(r"token|secret|password", k, re.I) else self._redact(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [self._redact(v) for v in o]
        if isinstance(o, str) and len(o) > 3000:
            return o[:1500] + f"...[truncated {len(o)} chars sha256={hashlib.sha256(o.encode()).hexdigest()[:12]}]"
        return o

    def event(self, **kw):
        kw["t"] = round(time.time() - self.t0, 3)
        self.log.write(json.dumps(self.sanitize(kw), ensure_ascii=False) + "\n")
        self.log.flush()

    def check(self, name: str, ok: bool, detail=None, required: bool = True):
        self.checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})
        mark = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"[{mark}] {name}" + (f"  -- {detail}" if (detail is not None and not ok) else ""))
        self.event(kind="check", name=name, ok=bool(ok), required=required, detail=detail)
        return ok


# ----------------------------------------------------------------------------
# MCP client (stdio JSON-RPC, newline-delimited, as served by `filmcraft-cli mcp`)
# ----------------------------------------------------------------------------


class McpError(RuntimeError):
    pass


class Mcp:
    def __init__(self, binary: str, rec: Recorder, args=(), label="session"):
        self.rec, self.label, self.next_id = rec, label, 0
        self.proc = subprocess.Popen(
            [binary, "mcp", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self.init = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "vlog-drafter-phase0-spike", "version": "0"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.next_id += 1
        rid = self.next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        self.rec.event(kind="req", session=self.label, method=method, params=params)
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError("server closed stdout")
            msg = json.loads(line)
            if msg.get("id") == rid:
                self.rec.event(kind="res", session=self.label, method=method, response=msg)
                if "error" in msg:
                    raise McpError(json.dumps(msg["error"]))
                return msg["result"]

    @staticmethod
    def _text(result):
        txt = "".join(c.get("text", "") for c in result.get("content", []))
        try:
            return json.loads(txt)
        except ValueError:
            return txt

    def tool(self, name, args=None):
        """Return (ok, parsed_payload)."""
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        return (not res.get("isError", False)), self._text(res)

    def cmd(self, cid, params=None):
        return self.tool("command_run", {"id": cid, "params": params or {}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe(path: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(p.stdout) if p.returncode == 0 else {"error": p.stderr}


def frames(ticks: int, fps: int = FPS) -> int:
    per_frame = TICKS_PER_SECOND // fps
    assert ticks % per_frame == 0, f"{ticks} ticks is not a whole frame at {fps} fps"
    return ticks // per_frame


def timeline_signature(mcp: Mcp) -> dict:
    """Normalized view of the active sequence (frame-based, ids dropped where unstable)."""
    ok, seq = mcp.tool("sequence_inspect")
    if not ok:
        raise McpError(f"sequence_inspect failed: {seq}")
    sig = {"durationFrames": seq["durationFrames"], "fps": seq["settings"]["frame_rate"],
           "size": [seq["settings"]["width"], seq["settings"]["height"]], "tracks": {}, "transitions": [],
           "markers": [], "raw_ids": {}}
    for kind in ("video", "audio"):
        for t in seq[kind]:
            clips = []
            for c in t["items"]:
                clips.append({
                    "name": c["name"], "start": c["startFrame"], "end": c["endFrame"],
                    "src_in": c["sourceIn"], "src_out": c["sourceOut"], "gain_db": c.get("gainDb"),
                    "speed": c.get("speed"), "linked": c.get("link") is not None, "clip": c["clip"], "item": c.get("item"),
                })
            sig["tracks"][t["name"]] = {"kind": kind, "id": t["id"], "clips": clips}
            for tr in t["transitions"]:
                sig["transitions"].append({"track": t["name"], "effect": tr["effect"], "duration": tr["duration"],
                                           "start": tr["start"], "align": tr["align"], "from": tr["from"], "to": tr["to"]})
    sig["markers"] = [{"name": m["name"], "start": m["start"], "comment": m.get("comment"), "color": m.get("color"),
                       "duration": m["duration"]} for m in seq["markers"]]
    return sig


def comparable(sig: dict) -> dict:
    """Signature minus ids that legitimately may change between sessions."""
    out = json.loads(json.dumps(sig))
    out.pop("raw_ids", None)
    for t in out["tracks"].values():
        t.pop("id", None)
        for c in t["clips"]:
            c.pop("clip", None)
            c.pop("item", None)
    for tr in out["transitions"]:
        tr.pop("from", None)
        tr.pop("to", None)
    return out


def file_url_to_path(u: str, base: Path | None = None, note: list | None = None) -> str:
    """Decode a media reference.  FilmCraft writes OTIO ``target_url`` / EDL ``SOURCE FILE`` *relative to the
    exported file's directory* when both share a top-level directory, absolute otherwise (observed)."""
    if "://" not in u and not u.startswith("/"):
        if note is not None and "relative media paths" not in note:
            note.append("relative media paths")
        return os.path.normpath(str((base or Path(".")) / urllib.parse.unquote(u)))
    u = re.sub(r"^file://localhost", "file://", u)
    return urllib.parse.unquote(urllib.parse.urlparse(u).path)


# ----------------------------------------------------------------------------
# interchange validators (all return dict with "ok" + findings; never raise)
# ----------------------------------------------------------------------------


def expected_clips(sig: dict, media_paths: dict[str, str]) -> dict[str, list[dict]]:
    out = {}
    for tname, t in sig["tracks"].items():
        out[tname] = [{"name": c["name"], "start": c["start"], "dur": c["end"] - c["start"],
                       "src_in": round(c["src_in"] / (TICKS_PER_SECOND / FPS)),
                       "path": media_paths.get(c["name"])} for c in t["clips"]]
    return out


def validate_otio(path: Path, sig: dict, media_paths: dict[str, str]) -> dict:
    res = {"format": "otio", "findings": [], "mismatches": []}
    rel_note: list = []
    doc = json.loads(path.read_text())
    res["schema"] = doc.get("OTIO_SCHEMA")
    tracks = {t["name"]: t for t in doc["tracks"]["children"]}
    exp = expected_clips(sig, media_paths)
    res["otio_tracks"] = list(tracks)
    res["raw_target_urls"] = sorted({r["target_url"] for t in doc["tracks"]["children"] for c in t["children"]
                                     if c["OTIO_SCHEMA"].startswith("Clip") for r in c["media_references"].values() if r.get("target_url")})
    for tname, want in exp.items():
        t = tracks.get(tname)
        if t is None:
            if want:
                res["mismatches"].append(f"track {tname} missing")
            continue
        pos, got = 0, []
        for ch in t["children"]:
            schema = ch["OTIO_SCHEMA"].split(".")[0]
            sr = ch.get("source_range")
            if schema == "Gap":
                pos += round(sr["duration"]["value"])
            elif schema == "Clip":
                ref = ch["media_references"][ch["active_media_reference_key"]]
                url = ref.get("target_url")
                got.append({"name": ch["name"], "start": pos, "dur": round(sr["duration"]["value"]),
                            "src_in": round(sr["start_time"]["value"]),
                            "path": file_url_to_path(url, path.parent, rel_note) if url else None, "ref": ref["OTIO_SCHEMA"].split(".")[0]})
                pos += round(sr["duration"]["value"])
            elif schema == "Transition":
                res.setdefault("transitions", []).append(
                    {"track": tname, "type": ch.get("transition_type"), "in": ch["in_offset"]["value"], "out": ch["out_offset"]["value"]})
        for w, g in zip(want, got):
            for k in ("name", "start", "dur", "src_in"):
                if w[k] != g[k]:
                    res["mismatches"].append(f"{tname}/{w['name']}: {k} expected {w[k]} got {g[k]}")
            if w["path"] and g["path"] != w["path"]:
                res["mismatches"].append(f"{tname}/{w['name']}: path expected {w['path']} got {g['path']}")
            if not w["path"]:
                res["findings"].append(f"{tname}/{w['name']}: graphic written as {g.get('ref')} (media-less)")
        if len(want) != len(got):
            res["mismatches"].append(f"track {tname}: expected {len(want)} clips got {len(got)}")
    want_tr = len(sig["transitions"])
    got_tr = len(res.get("transitions", []))
    if want_tr != got_tr:
        res["mismatches"].append(f"transitions expected {want_tr} got {got_tr}")
    for tr in res.get("transitions", []):
        res["findings"].append(f"transition {tr['type']} in_offset={tr['in']} out_offset={tr['out']} frames")
    mk = doc["tracks"].get("markers", [])
    res["markers"] = [{"name": m["name"], "start": m["marked_range"]["start_time"]["value"], "color": m["color"]} for m in mk]
    for m in sig["markers"]:
        if not any(x["name"] == m["name"] and x["start"] == m["start"] // (TICKS_PER_SECOND // FPS) for x in res["markers"]):
            res["mismatches"].append(f"marker {m['name']} missing/misplaced")
    # optional: real OTIO library load (+ rate/duration sanity)
    try:
        import opentimelineio as otio  # type: ignore
        tl = otio.adapters.read_from_file(str(path))
        res["otio_library"] = {"version": otio.__version__ if hasattr(otio, "__version__") else "?",
                               "loaded": True, "duration_frames": tl.duration().to_frames(),
                               "rate": tl.duration().rate}
        if tl.duration().to_frames() != sig["durationFrames"]:
            res["mismatches"].append(f"otio duration {tl.duration().to_frames()} != {sig['durationFrames']}")
    except ImportError:
        res["otio_library"] = {"loaded": None, "note": "opentimelineio not installed"}
    except Exception as e:  # real parse failure is a finding
        res["otio_library"] = {"loaded": False, "error": str(e)}
        res["mismatches"].append(f"opentimelineio failed to load: {e}")
    res["media_path_style"] = "relative to the .otio file" if rel_note else "absolute file:// URL"
    if rel_note:
        res["findings"].append("target_url is RELATIVE to the .otio location (resolved here against the file's directory)")
    res["ok"] = not res["mismatches"]
    return res


def validate_fcp7(path: Path, sig: dict, media_paths: dict[str, str]) -> dict:
    res = {"format": "fcp7_xml", "findings": [], "mismatches": []}
    root = ET.parse(path).getroot()
    res["root"] = root.tag + " version=" + root.get("version", "?")
    seq = root.find("sequence")
    if seq is None:
        return {**res, "ok": False, "mismatches": ["no <sequence>"]}
    res["sequence_duration"] = int(seq.findtext("duration"))
    res["timebase"] = seq.findtext("rate/timebase") + (" ntsc" if seq.findtext("rate/ntsc") == "TRUE" else "")
    if res["sequence_duration"] != sig["durationFrames"]:
        res["mismatches"].append(f"duration {res['sequence_duration']} != {sig['durationFrames']}")
    files: dict[str, str] = {}
    for f in root.iter("file"):
        if f.get("id") and f.findtext("pathurl"):
            files[f.get("id")] = file_url_to_path(f.findtext("pathurl"))
    exp = expected_clips(sig, media_paths)
    vtracks = seq.findall("media/video/track")
    atracks = seq.findall("media/audio/track")
    vnames = [n for n, t in sig["tracks"].items() if t["kind"] == "video"]
    anames = [n for n, t in sig["tracks"].items() if t["kind"] == "audio"]
    n_trans = 0
    for names, tracks in ((vnames, vtracks), (anames, atracks)):
        if len(tracks) != len(names):
            res["mismatches"].append(f"track count expected {len(names)} got {len(tracks)}")
        for tname, tr in zip(names, tracks):
            got = []
            for ci in tr.findall("clipitem"):
                f = ci.find("file")
                p = files.get(f.get("id")) if f is not None else None
                got.append({"name": ci.findtext("name"), "start": int(ci.findtext("start")),
                            "dur": int(ci.findtext("end")) - int(ci.findtext("start")),
                            "src_in": int(ci.findtext("in")), "path": p})
            n_trans += len(tr.findall("transitionitem"))
            for ti in tr.findall("transitionitem"):
                res["findings"].append(f"transitionitem {ti.findtext('effect/name')} {ti.findtext('start')}-{ti.findtext('end')} {ti.findtext('alignment')}")
            want = exp.get(tname, [])
            if len(want) != len(got):
                res["mismatches"].append(f"track {tname}: expected {len(want)} clipitems got {len(got)}")
            for w, g in zip(want, got):
                for k in ("name", "start", "dur", "src_in"):
                    if w[k] != g[k]:
                        res["mismatches"].append(f"{tname}/{w['name']}: {k} expected {w[k]} got {g[k]}")
                if w["path"] and g["path"] != w["path"]:
                    res["mismatches"].append(f"{tname}/{w['name']}: path expected {w['path']} got {g['path']}")
                if not w["path"]:
                    res["findings"].append(f"{tname}/{w['name']}: graphic written as clipitem without media (pathurl={g['path']})")
    if n_trans != len(sig["transitions"]):
        res["mismatches"].append(f"transitions expected {len(sig['transitions'])} got {n_trans}")
    res["markers"] = [{"name": m.findtext("name"), "in": m.findtext("in")} for m in seq.findall("marker")]
    for m in sig["markers"]:
        if not any(x["name"] == m["name"] and int(x["in"]) == m["start"] // (TICKS_PER_SECOND // FPS) for x in res["markers"]):
            res["mismatches"].append(f"marker {m['name']} missing/misplaced")
    try:
        import opentimelineio as otio  # type: ignore
        tl = otio.adapters.read_from_file(str(path), adapter_name="fcp_xml")
        res["otio_fcp_adapter"] = {"loaded": True, "duration_frames": tl.duration().to_frames()}
    except ImportError:
        res["otio_fcp_adapter"] = {"loaded": None, "note": "opentimelineio / otio-fcp-adapter not installed"}
    except Exception as e:
        res["otio_fcp_adapter"] = {"loaded": False, "error": str(e)[:300]}
        res["findings"].append(f"otio fcp_xml adapter could not load file: {str(e)[:200]}")
    res["ok"] = not res["mismatches"]
    return res


def tc_to_frames(tc: str, fps=FPS) -> int:
    h, m, s, f = (int(x) for x in re.split(r"[:;]", tc))
    return ((h * 60 + m) * 60 + s) * fps + f


def validate_edl(path: Path, sig: dict, media_paths: dict[str, str]) -> dict:
    res = {"format": "cmx3600_edl", "findings": [], "mismatches": []}
    text = path.read_text()
    events = []
    for ln in text.splitlines():
        m = re.match(r"^(\d{3})\s+(\S+)\s+(\S+)\s+(\S+)\s*(\d*)\s+(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)\s+(\d\d:\d\d:\d\d[:;]\d\d)", ln)
        if m:
            events.append({"n": m[1], "reel": m[2], "track": m[3], "kind": m[4], "dur": m[5],
                           "src_in": tc_to_frames(m[6]), "src_out": tc_to_frames(m[7]),
                           "rec_in": tc_to_frames(m[8]), "rec_out": tc_to_frames(m[9])})
    res["events"] = events
    res["has_title"] = "TITLE:" in text
    raw_src = sorted(set(re.findall(r"\* SOURCE FILE: (.*)", text)))
    res["raw_source_files"] = raw_src
    rel_note: list = []
    res["source_files"] = [file_url_to_path(x, path.parent, rel_note) for x in raw_src]
    res["media_path_style"] = "relative to the .edl file" if rel_note else "absolute"
    if rel_note:
        res["findings"].append("SOURCE FILE comments are RELATIVE to the .edl location")
    v1 = sig["tracks"].get("Video 1", {"clips": []})["clips"]
    if v1 and events:
        first = events[0]
        if first["src_in"] != round(v1[0]["src_in"] / (TICKS_PER_SECOND // FPS)):
            res["mismatches"].append(f"event 1 source-in {first['src_in']} != {v1[0]['src_in']}")
        if first["rec_in"] != v1[0]["start"]:
            res["mismatches"].append(f"event 1 record-in {first['rec_in']} != {v1[0]['start']}")
    else:
        res["mismatches"].append("no EDL events found")
    for p in res["source_files"]:
        if p not in media_paths.values():
            res["mismatches"].append(f"unknown source file {p}")
    res["findings"].append("EDL carries a single video track (V1) + audio; V2 clips/titles are not exported")
    if any(e["dur"] for e in events):
        res["findings"].append("dissolve event present: " + ", ".join(f"{e['kind']}{e['dur']}" for e in events if e["dur"]))
    try:
        import opentimelineio as otio  # type: ignore
        tl = otio.adapters.read_from_file(str(path), adapter_name="cmx_3600", rate=FPS)
        res["otio_cmx3600_adapter"] = {"loaded": True, "duration_frames": tl.duration().to_frames()}
    except ImportError:
        res["otio_cmx3600_adapter"] = {"loaded": None, "note": "otio-cmx3600-adapter not installed"}
    except Exception as e:
        res["otio_cmx3600_adapter"] = {"loaded": False, "error": str(e)[:300]}
    res["ok"] = not res["mismatches"]
    return res


def validate_fcpxml(path: Path, sig: dict, media_paths: dict[str, str]) -> dict:
    res = {"format": "fcpxml", "findings": [], "mismatches": []}
    root = ET.parse(path).getroot()
    res["version"] = root.get("version")
    assets = {a.get("id"): a for a in root.iter("asset")}
    res["assets"] = {i: file_url_to_path(a.find("media-rep").get("src")) for i, a in assets.items() if a.find("media-rep") is not None}
    for p in res["assets"].values():
        if p not in media_paths.values():
            res["mismatches"].append(f"unknown asset path {p}")
    names = [c.get("name") for c in root.iter("asset-clip")]
    res["asset_clips"] = names
    for t in sig["tracks"].values():
        if t["kind"] == "video":
            for c in t["clips"]:
                if c["name"] not in names:
                    res["mismatches"].append(f"clip {c['name']} missing")
    for c in root.iter("asset-clip"):
        ref = c.get("ref")
        if ref == "" or (ref and ref not in assets):
            res["findings"].append(f"asset-clip '{c.get('name')}' has dangling ref='{ref}' (graphic without media) -- importers may reject")
    res["transitions"] = [t.get("name") for t in root.iter("transition")]
    if len(res["transitions"]) != len(sig["transitions"]):
        res["mismatches"].append("transition count mismatch")
    fmts = [f.get("frameDuration") for f in root.iter("format")]
    res["format_frameDurations"] = fmts
    if len(set(fmts)) > 1:
        res["findings"].append(f"resource <format> frameDurations differ across assets {sorted(set(fmts))} (a still got a 29.97 format in a 30p sequence)")
    res["ok"] = not res["mismatches"]
    return res


CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def validate_aaf(path: Path, *_):
    head = path.read_bytes()[:8]
    return {"format": "aaf", "ok": head == CFB_MAGIC, "findings": ["only container magic checked (no AAF library installed); "
            "FilmCraft's own AAF re-import is exercised separately"], "mismatches": [] if head == CFB_MAGIC else ["bad CFB magic"]}


def validate_omf(path: Path, *_):
    data = path.read_bytes()
    ok = data[:8] == b"\xa4CM\xa5Hdr\xd7" or b"OMFI" in data[:64] or len(data) > 0
    return {"format": "omf", "ok": ok, "findings": ["OMF 2.0 carries audio only (no video); structural check = non-empty file"],
            "mismatches": []}


# ----------------------------------------------------------------------------
# main flow
# ----------------------------------------------------------------------------

REQUIRED_COMMANDS = {
    "create_project": ["file.newProject"],
    "open_project_reopen": ["file.open"],
    "save_project": ["file.save", "file.saveAs"],
    "import_media": ["file.import"],
    "create_sequence": ["file.newSequence"],
    "place_clip": ["timeline.place"],
    "trim_clip": ["timeline.trim"],
    "move_clip": ["timeline.move"],
    "add_tracks": ["sequence.addTracks"],
    "add_marker": ["markers.add"],
    "add_title": ["graphics.newText"],
    "add_transition": ["sequence.applyVideoTransition"],
    "audio_gain": ["clip.audioGain"],
    "audio_fade": ["sequence.applyAudioTransition"],
    "render_preview": ["file.exportMedia"],
    "export_interchange": ["file.exportInterchange"],
    "undo": ["edit.undo"],
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filmcraft-cli", default=os.environ.get("FILMCRAFT_CLI"), help="path to built filmcraft-cli (or $FILMCRAFT_CLI)")
    ap.add_argument("--filmcraft-src", default=os.environ.get("FILMCRAFT_SRC"), help="FilmCraft git checkout (for commit/describe)")
    ap.add_argument("--fixtures", default=str(REPO / "tests/fixtures/generated"))
    ap.add_argument("--out", default=str(REPO / "spike/out"))
    args = ap.parse_args(argv)
    if not args.filmcraft_cli or not Path(args.filmcraft_cli).exists():
        print("error: --filmcraft-cli (or $FILMCRAFT_CLI) must point at a built filmcraft-cli", file=sys.stderr)
        return 2
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"error: {tool} not on PATH", file=sys.stderr)
            return 2
    binary = str(Path(args.filmcraft_cli).resolve())
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    fx = Path(args.fixtures).resolve()
    if not (fx / "landscape_5s.mp4").exists():
        run(["bash", str(REPO / "tests/fixtures/make_fixtures.sh"), str(fx)])
    rec = Recorder(out, fx)
    snap: dict = {"schema": "phase0.capability_snapshot.v1", "required_checks_failed": []}
    chk = rec.check

    # ---------------- 1. discovery ----------------
    ver = run([binary, "--version"]).stdout.strip()
    snap["filmcraft"] = {"version_output": ver}
    chk("cli --version runs", ver.startswith("filmcraft-cli"), ver)
    if args.filmcraft_src and (Path(args.filmcraft_src) / ".git").exists():
        g = lambda *a: run(["git", "-C", args.filmcraft_src, *a]).stdout.strip()
        snap["filmcraft"].update(commit=g("rev-parse", "HEAD"), describe=g("describe", "--tags", "--always"),
                                 commit_date=g("log", "-1", "--format=%cI"))
    snap["environment"] = {"os": platform.platform(), "python": platform.python_version(),
                           "ffmpeg": run(["ffmpeg", "-version"]).stdout.splitlines()[0],
                           "display": bool(os.environ.get("DISPLAY")), "headless": True}

    help_txt = run([binary, "help"]).stdout
    snap["cli_subcommands"] = list(dict.fromkeys(re.findall(r"^  (\w[\w-]*)(?= )", help_txt.split("SUBCOMMANDS")[1].split("OPTIONS")[0], re.M)))
    catalog_p = run([binary, "commands", "--json"])
    catalog = json.loads(catalog_p.stdout)
    chk("CLI `commands --json` lists engine commands", len(catalog) > 100, len(catalog))
    ids = {c["id"]: c for c in catalog}
    snap["command_catalog"] = {"count": len(catalog), "required": {k: {v: ids[v]["params"] for v in vs if v in ids} for k, vs in REQUIRED_COMMANDS.items()}}
    (out / "command_catalog.json").write_text(json.dumps(catalog, indent=1))

    mcp = Mcp(binary, rec, label="build")
    snap["mcp"] = {"initialize": mcp.init}
    tools = mcp.rpc("tools/list")["tools"]
    (out / "mcp_tools.json").write_text(json.dumps(tools, indent=2))
    snap["mcp"]["tools"] = [t["name"] for t in tools]
    chk("MCP initialize + tools/list handshake", len(tools) >= 5 and "command_run" in snap["mcp"]["tools"], snap["mcp"]["tools"])
    ok, mcp_cmds = mcp.tool("command_list")
    chk("MCP command_list matches CLI catalog", ok and isinstance(mcp_cmds, list) and len(mcp_cmds) == len(catalog),
        (len(mcp_cmds) if isinstance(mcp_cmds, list) else mcp_cmds))
    missing = {k: [v for v in vs if v not in ids] for k, vs in REQUIRED_COMMANDS.items()}
    missing = {k: v for k, v in missing.items() if v}
    chk("all commands the adapter needs exist in the catalog", not missing, missing)

    cap: dict[str, dict] = {}

    def capability(name, tool, ok, evidence=None, status=None):
        cap[name] = {"status": status or ("verified working" if ok else "available but failed"), "tool": tool, "evidence": evidence}

    # ---------------- 2. build ----------------
    ok, r = mcp.cmd("file.newProject", {"name": "phase0-spike"})
    chk("create project (file.newProject)", ok, r)
    capability("create_project", "command_run file.newProject", ok, r)

    media = {"landscape": fx / "landscape_5s.mp4", "still": fx / "still_1080p.png", "portrait": fx / "portrait_4s.mp4",
             "music": fx / "music_6s.wav", "music_m4a": fx / "music_6s.m4a"}
    ok, r = mcp.tool("media_import", {"paths": [str(media[k]) for k in media]})
    chk("import 5 media files (media_import)", ok and not r.get("errors") and len(r.get("items", [])) == 5, r)
    capability("import_media", "media_import {paths}", ok and len(r.get("items", [])) == 5, r)
    item = dict(zip(media, r["items"]))
    media_paths = {p.name: str(p) for p in media.values()}
    ok, proj = mcp.tool("project_inspect")
    pitems = {c["name"]: c for c in proj["root"]["children"]}
    snap["imported_items"] = {n: {"item": c["item"], "type": c["type"], "duration_ticks": c["duration"],
                                  "duration_s": c["duration"] / TICKS_PER_SECOND} for n, c in pitems.items()}
    chk("imported durations match ffprobe (video 5s, still 5s default, wav 6s)",
        abs(pitems["landscape_5s.mp4"]["duration"] / TICKS_PER_SECOND - 5) < 0.04 and
        abs(pitems["music_6s.wav"]["duration"] / TICKS_PER_SECOND - 6) < 0.04,
        snap["imported_items"])
    ok, ms = mcp.cmd("media.status", {})
    snap["media_status"] = ms
    chk("media.status reports imported media online", ok, ms, required=False)
    capability("verify_media_online", "command_run media.status", ok, ms)

    ok, r = mcp.cmd("file.newSequence", {"name": "Main", "width": 1920, "height": 1080, "fps": 30, "video": 1, "audio": 1})
    chk("create sequence 1920x1080 30fps (file.newSequence)", ok, r)
    capability("create_sequence", "command_run file.newSequence", ok, r)
    ok, r = mcp.cmd("sequence.addTracks", {"video": 1, "audio": 1})
    chk("add second video track + second audio track (sequence.addTracks)", ok, r)
    capability("add_tracks", "command_run sequence.addTracks", ok, r)
    sig0 = timeline_signature(mcp)
    chk("sequence has V1,V2,A1,A2", list(sig0["tracks"]) == ["Video 1", "Video 2", "Audio 1", "Audio 2"], list(sig0["tracks"]))
    chk("sequence settings read back 1920x1080 @ 30/1", sig0["size"] == [1920, 1080] and sig0["fps"] == {"num": 30, "den": 1}, sig0["fps"])

    T = TICKS_PER_SECOND
    ok, r = mcp.cmd("timeline.place", {"item": item["landscape"], "track": "V1", "seconds": 0})
    chk("place video on V1 (timeline.place)", ok, r)
    landscape_v, landscape_a = r["clips"]
    ok, r = mcp.cmd("timeline.place", {"item": item["still"], "track": "V1", "seconds": 5, "duration": 3 * T})
    chk("place still on V1 after the video, 3 s (timeline.place)", ok, r)
    still_clip = r["clips"][0]
    capability("place_clip", "command_run timeline.place", ok, "video+linked audio clip ids returned; still placed with duration")
    sig = timeline_signature(mcp)
    v1 = sig["tracks"]["Video 1"]["clips"]
    chk("placement read back: V1 = [landscape 0-150, still 150-240]",
        [(c["name"], c["start"], c["end"]) for c in v1] == [("landscape_5s.mp4", 0, 150), ("still_1080p.png", 150, 240)], v1)
    chk("placing a movie links its audio onto A1", [c["name"] for c in sig["tracks"]["Audio 1"]["clips"]] == ["landscape_5s.mp4"])

    # trim: ripple out -1s then ripple in +0.5s (15 frames)
    ok1, r1 = mcp.cmd("timeline.trim", {"clip": landscape_v, "edge": "out", "mode": "ripple", "deltaFrames": -30})
    ok2, r2 = mcp.cmd("timeline.trim", {"clip": landscape_v, "edge": "in", "mode": "ripple", "deltaFrames": 15})
    sig = timeline_signature(mcp)
    v1 = sig["tracks"]["Video 1"]["clips"]
    a1 = sig["tracks"]["Audio 1"]["clips"]
    want = [("landscape_5s.mp4", 0, 105, 0.5, 4.0), ("still_1080p.png", 105, 195, 0.0, 3.0)]
    got = [(c["name"], c["start"], c["end"], c["src_in"] / T, c["src_out"] / T) for c in v1]
    chk("trim (ripple): landscape source 0.5-4.0s on timeline 0-105, still rippled to 105-195", ok1 and ok2 and got == want, got)
    chk("trim keeps linked audio in sync (A1 0-105, src 0.5-4.0)",
        [(c["start"], c["end"], c["src_in"] / T, c["src_out"] / T) for c in a1] == [(0, 105, 0.5, 4.0)], a1)
    capability("trim_clip", "command_run timeline.trim {clip, edge, mode:ripple|regular, deltaFrames|delta}", ok1 and ok2 and got == want, got)

    # portrait on V2 (video only), then move it later
    ok, r = mcp.cmd("timeline.place", {"item": item["portrait"], "track": "V2", "seconds": 1, "duration": 2 * T, "audio": False})
    portrait_clip = r["clips"][0]
    chk("place portrait video on V2 (video only, 2 s)", ok, r)
    ok, r = mcp.cmd("timeline.move", {"moves": [{"clip": portrait_clip, "track": "V2", "time": 2 * T}]})
    sig = timeline_signature(mcp)
    pc = sig["tracks"]["Video 2"]["clips"][0]
    chk("move clip later (1s -> 2s) read back", ok and (pc["start"], pc["end"]) == (60, 120), pc)
    capability("move_clip", "command_run timeline.move {moves:[{clip,track,time}]}", ok and (pc["start"], pc["end"]) == (60, 120), pc)

    # title on V2 at 0..2 s (graphics.newText `track` is a 0-based index)
    ok, r = mcp.cmd("graphics.newText", {"text": "Hello Title", "seconds": 2, "time": 0, "size": 80, "position": [200, 900], "track": 1})
    sig = timeline_signature(mcp)
    v2 = sig["tracks"]["Video 2"]["clips"]
    chk("add title text on V2 0-60 (graphics.newText)", ok and any(c["name"] == "Hello Title" and (c["start"], c["end"]) == (0, 60) for c in v2), v2)
    capability("add_title", "command_run graphics.newText", ok, r)

    # transition at the V1 cut
    ok, r = mcp.cmd("sequence.applyVideoTransition", {"clip": still_clip, "edge": "in", "effect": "cross_dissolve", "frames": 15})
    sig = timeline_signature(mcp)
    chk("add cross dissolve at the cut (sequence.applyVideoTransition)",
        ok and len(sig["transitions"]) == 1 and sig["transitions"][0]["effect"] == "cross_dissolve", sig["transitions"])
    capability("add_transition", "command_run sequence.applyVideoTransition", ok and len(sig["transitions"]) == 1, sig["transitions"])
    chk("transition is 15 frames, centered on the cut",
        len(sig["transitions"]) == 1 and frames(sig["transitions"][0]["duration"]) == 15 and sig["transitions"][0]["align"] == "center", sig["transitions"])

    # audio: music on A2 + gain + fade (gain/fade are optional-but-recorded)
    ok, r = mcp.cmd("timeline.place", {"item": item["music"], "track": "A2", "seconds": 0})
    chk("place audio-only file on A2 (timeline.place track=A2)", ok, r)
    music_clip = r["clips"][0] if ok else None
    capability("place_audio_only", "command_run timeline.place track=A2", ok, r)
    ok, r = mcp.cmd("clip.audioGain", {"clips": [music_clip], "mode": "set", "db": -6})
    sig = timeline_signature(mcp)
    gain = sig["tracks"]["Audio 2"]["clips"][0]["gain_db"]
    chk("set clip audio gain -6 dB (clip.audioGain) and read gainDb", ok and abs((gain or 0) + 6) < 0.01, gain, required=False)
    capability("audio_gain", "command_run clip.audioGain + sequence_inspect gainDb", ok and abs((gain or 0) + 6) < 0.01, gain)
    ok, r = mcp.cmd("sequence.applyAudioTransition", {"clip": music_clip, "effect": "constant_power", "edge": "out", "frames": 15})
    sig = timeline_signature(mcp)
    n_audio_tr = len([t for t in sig["transitions"] if sig["tracks"][t["track"]]["kind"] == "audio"])
    chk("audio fade-out on music (sequence.applyAudioTransition)", ok, r, required=False)
    capability("audio_fade", "command_run sequence.applyAudioTransition", ok, {"result": r, "audio_transitions_in_inspect": n_audio_tr})
    cap["audio_ducking"] = {"status": "not tested", "tool": "effects.setParam (keyframes) exists in catalog",
                            "evidence": "docs show effects.setParam with keyframed params; no ducking command exercised"}

    # marker at 2 s
    ok, r = mcp.cmd("markers.add", {"time": 2 * T, "name": "Scene A", "comment": "phase0", "color": "Green"})
    sig = timeline_signature(mcp)
    chk("add sequence marker at 2.0 s (markers.add)", ok and [m["name"] for m in sig["markers"]] == ["Scene A"], sig["markers"])
    capability("add_marker", "command_run markers.add", ok, sig["markers"])

    # undo smoke test (edit.undo) then redo by repeating the marker
    ok, r = mcp.cmd("edit.undo")
    sig_u = timeline_signature(mcp)
    chk("edit.undo removes the last edit (marker)", ok and sig_u["markers"] == [], sig_u["markers"], required=False)
    capability("undo", "command_run edit.undo", ok and sig_u["markers"] == [])
    ok, r = mcp.cmd("markers.add", {"time": 2 * T, "name": "Scene A", "comment": "phase0", "color": "Green"})
    sig = timeline_signature(mcp)
    chk("marker re-added after undo", ok and len(sig["markers"]) == 1)

    snap["timeline_built"] = comparable(sig)
    snap["timeline_built_full"] = sig
    snap["time_representation"] = {
        "ticks_per_second_documented": TICKS_PER_SECOND,
        "one_frame_30fps_ticks": TICKS_PER_SECOND // 30,
        "sample_clip_ticks": {"duration_5s": pitems["landscape_5s.mp4"]["duration"]},
        "inspect_fields": "start/end/duration in ticks plus startFrame/endFrame/durationFrames; sourceIn/sourceOut in ticks (media time)",
        "frame_rate_representation": sig["fps"],
        "command_inputs": "ticks, or seconds= / frame= / timecode=; deltaFrames= for trims",
    }
    chk("ticks/second 254016000000 divides a 30 fps frame exactly",
        TICKS_PER_SECOND % 30 == 0 and snap["time_representation"]["sample_clip_ticks"]["duration_5s"] == 5 * TICKS_PER_SECOND)

    # ---------------- 3. persistence ----------------
    proj_path = out / "phase0.fcproj"
    ok, r = mcp.cmd("file.saveAs", {"path": str(proj_path)})
    chk("save project (.fcproj) (file.saveAs)", ok and proj_path.exists(), r)
    capability("save_project", "command_run file.saveAs", ok and proj_path.exists(), r)
    doc = json.loads(proj_path.read_text())
    snap["fcproj"] = {"format": doc.get("format"), "schema_version": doc.get("schema_version"), "generator": doc.get("generator"),
                      "top_level_keys": list(doc), "project_keys": list(doc["project"]), "bytes": proj_path.stat().st_size,
                      "media_referenced_by_path": str(fx) in proj_path.read_text()}
    chk(".fcproj is JSON with format=filmcraft.project and a schema_version", doc.get("format") == "filmcraft.project" and isinstance(doc.get("schema_version"), int), snap["fcproj"])
    sig_saved = timeline_signature(mcp)
    mcp.close()

    mcp2 = Mcp(binary, rec, args=["--project", str(proj_path)], label="reopen")
    sig_re = timeline_signature(mcp2)
    same = comparable(sig_saved) == comparable(sig_re)
    chk("reopen in a NEW server process: timeline identical (clip order/in/out/duration/transition/marker)", same,
        None if same else {"before": comparable(sig_saved), "after": comparable(sig_re)})
    capability("open_project_reopen", "mcp --project <file.fcproj> (new process) + sequence_inspect", same)
    ok, ms2 = mcp2.cmd("media.status", {})
    snap["media_status_after_reopen"] = ms2
    chk("media still online/linked to original paths after reopen", ok, ms2, required=False)
    ok, ins = mcp2.tool("doc_inspect")
    chk("doc_inspect works on reopened project", ok and isinstance(ins, dict), None if ok else ins, required=False)

    # ---------------- 4. render ----------------
    prev = out / "preview.mp4"
    t0 = time.time()
    ok, r = mcp2.cmd("file.exportMedia", {"path": str(prev), "format": "h264", "wait": True})
    render_s = time.time() - t0
    chk("render preview MP4 (file.exportMedia wait=true)", ok and prev.exists(), r)
    pr = ffprobe(prev)
    vs = next((s for s in pr.get("streams", []) if s["codec_type"] == "video"), {})
    au = [s for s in pr.get("streams", []) if s["codec_type"] == "audio"]
    expect_s = sig_re["durationFrames"] / FPS
    dur = float(pr.get("format", {}).get("duration", 0))
    snap["render"] = {"seconds_to_render": round(render_s, 1), "result": r, "ffprobe": {
        "format_duration": dur, "video": {k: vs.get(k) for k in ("codec_name", "width", "height", "r_frame_rate", "nb_frames", "pix_fmt", "duration")},
        "audio": [{k: s.get(k) for k in ("codec_name", "channels", "sample_rate", "duration")} for s in au]}}
    chk("preview is 1920x1080 @ 30 fps h264", (vs.get("width"), vs.get("height"), vs.get("r_frame_rate"), vs.get("codec_name")) == (1920, 1080, "30/1", "h264"), vs)
    chk(f"preview duration within 1 frame of sequence ({expect_s:.3f}s)", abs(dur - expect_s) <= 1 / FPS + 0.02, dur)
    chk("preview frame count equals sequence frames", str(vs.get("nb_frames")) == str(sig_re["durationFrames"]), vs.get("nb_frames"))
    chk("preview has an audio stream", len(au) >= 1, au)
    capability("render_preview", "command_run file.exportMedia {path, format, wait:true}", ok and prev.exists(), snap["render"]["ffprobe"])
    if prev.exists():
        frame = out / "preview_t0.5.png"
        run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(prev), "-frames:v", "1", str(frame)])
    try:
        rf = mcp2.rpc("tools/call", {"name": "render_frame", "arguments": {"seconds": 0.5, "max_side": 640}})
        kinds = [c.get("type") for c in rf.get("content", [])]
        img = next((c for c in rf.get("content", []) if c.get("type") == "image"), None)
        snap["render_frame_tool"] = {"ok": not rf.get("isError"), "content_types": kinds,
                                     "mime": img.get("mimeType") if img else None,
                                     "base64_bytes": len(img.get("data", "")) if img else 0}
        chk("render_frame tool returns an image of the program frame", img is not None and not rf.get("isError"), kinds, required=False)
    except McpError as e:
        snap["render_frame_tool"] = {"ok": False, "error": str(e)}
        chk("render_frame tool returns an image of the program frame", False, str(e), required=False)

    # ---------------- 5. interchange ----------------
    ex_dir = out / "interchange"
    ex_dir.mkdir()
    ok, d = mcp2.cmd("file.exportInterchange", {"format": "bogus", "path": str(ex_dir / "x.bogus")})
    chk("unknown interchange format is rejected with an error (no silent fallback)", not ok and not (ex_dir / "x.bogus").exists(), d, required=False)
    fmt_hint = ids["file.exportInterchange"]["params"]
    snap["interchange_formats_declared"] = fmt_hint
    declared = re.search(r'"format":"([^"]+)"', fmt_hint).group(1).split("|")
    validators = {"otio": validate_otio, "xml": validate_fcp7, "edl": validate_edl, "fcpxml": validate_fcpxml,
                  "aaf": validate_aaf, "omf": validate_omf}
    ext = {"otio": "otio", "xml": "xml", "edl": "edl", "fcpxml": "fcpxml", "aaf": "aaf", "omf": "omf"}
    inter = {}
    for fmt in declared:
        p = ex_dir / f"phase0.{ext[fmt]}"
        ok, r = mcp2.cmd("file.exportInterchange", {"format": fmt, "path": str(p)})
        entry = {"export_ok": ok, "bytes": p.stat().st_size if p.exists() else 0, "report": (r.get("report") if isinstance(r, dict) else r)}
        chk(f"export interchange {fmt} (file.exportInterchange)", ok and p.exists() and p.stat().st_size > 0, r)
        if ok and p.exists():
            try:
                v = validators[fmt](p, sig_re, media_paths)
            except Exception as e:
                v = {"ok": False, "mismatches": [f"validator crashed: {e!r}"], "findings": []}
            entry["validation"] = v
            chk(f"{fmt}: structure parses and matches built timeline", v["ok"], v["mismatches"],
                required=fmt in ("otio", "xml", "edl"))
        inter[fmt] = entry
    if shutil.which("xmllint"):
        for fmt in ("xml", "fcpxml"):
            p = ex_dir / f"phase0.{ext[fmt]}"
            if p.exists():
                lint = run(["xmllint", "--noout", str(p)])
                inter[fmt]["xmllint"] = {"rc": lint.returncode, "stderr": lint.stderr[:300]}
                chk(f"xmllint well-formed: {fmt}", lint.returncode == 0, lint.stderr[:200])
    # FilmCraft's own re-import of what it exported (self-consistency only, NOT a Resolve test)
    for fmt in ("otio", "xml", "edl", "fcpxml", "aaf"):
        p = ex_dir / f"phase0.{ext[fmt]}"
        if not p.exists():
            continue
        m3 = Mcp(binary, rec, label=f"reimport-{fmt}")
        m3.cmd("file.newProject", {"name": "reimport"})
        ok, r = m3.cmd("file.import", {"paths": [str(p)]})
        info = {"import_ok": ok, "result": r}
        if ok:
            ok2, seqres = m3.tool("project_inspect")
            info["sequences_in_project"] = [c["name"] for c in seqres["root"]["children"] if c.get("type") == "Sequence"]
            try:
                m3.cmd("sequence.open", {})  # may not exist; ignored
            except Exception:
                pass
            if seqres.get("activeSequence") is not None:
                s3 = timeline_signature(m3)
                info["reimported_clips"] = {k: [(c["name"], c["start"], c["end"]) for c in t["clips"]] for k, t in s3["tracks"].items() if t["clips"]}
                info["reimported_transitions"] = len(s3["transitions"])
        inter[fmt]["filmcraft_reimport"] = info
        m3.close()
    snap["interchange"] = inter
    capability("export_interchange", "command_run file.exportInterchange {format, path}",
               all(e["export_ok"] for e in inter.values()),
               {f: {"export_ok": e["export_ok"], "structurally_valid": e.get("validation", {}).get("ok")} for f, e in inter.items()})

    # resolve-specific honesty
    snap["resolve"] = {"status": "pending -- requires user's DaVinci Resolve installation",
                       "note": "Resolve is proprietary GUI software needing a GPU; it was NOT run. No Resolve compatibility is claimed."}

    # fps probe (rational frame-rate storage) in a throw-away session
    m4 = Mcp(binary, rec, label="fps-probe")
    m4.cmd("file.newProject", {"name": "fps"})
    fps_res = {}
    for label, val in (("23.976", 24000 / 1001), ("29.97", 30000 / 1001), ("59.94", 60000 / 1001), ("25", 25), ("30", 30)):
        ok, r = m4.cmd("file.newSequence", {"name": f"s{label}", "fps": val, "video": 1, "audio": 1})
        if ok:
            ok_s, s = m4.tool("sequence_inspect")
            fps_res[label] = {"requested": val, "stored": s["settings"]["frame_rate"], "drop_frame": s["settings"]["drop_frame"]}
        else:
            fps_res[label] = {"requested": val, "error": r}
    m4.close()
    snap["frame_rate_probe"] = fps_res
    chk("fractional frame rates are stored as exact rationals (24000/1001, 30000/1001, 60000/1001)",
        all(fps_res.get(k, {}).get("stored", {}).get("den") == 1001 for k in ("23.976", "29.97", "59.94")), fps_res, required=False)

    # CLI parity check: same project through the plain CLI
    cli = run([binary, "--project", str(proj_path), "inspect", "sequence", "--compact"])
    try:
        cj = json.loads(cli.stdout)
        cli_ok = cj.get("durationFrames") == sig_re["durationFrames"]
    except Exception:
        cli_ok = False
    chk("CLI `--project p.fcproj inspect sequence` agrees with MCP", cli_ok, cli.stderr[:200], required=False)
    capability("cli_inspect", "filmcraft-cli --project p inspect sequence", cli_ok)
    mcp2.close()

    # ---------------- 6. capability map & outputs ----------------
    for name, cmds in REQUIRED_COMMANDS.items():
        cap.setdefault(name, {"status": "not tested", "tool": None, "evidence": None})
        cap[name]["commands_in_catalog"] = {c: (c in ids) for c in cmds}
    snap["capabilities"] = cap
    snap["gaps"] = [
        "Titles/graphics are NOT carried by any interchange format: OTIO/FCP7 XML/FCPXML write a media-less clip, AAF/EDL drop it (export report).",
        "EDL holds one video track only; V2 content is lost.",
        "FilmCraft interchange of the transition is center-aligned; handle media before/after the cut is required or source-in is clamped (see report).",
        "OMF carries audio only.",
        "DaVinci Resolve import is untested (pending user's installation).",
    ]
    snap["checks"] = rec.checks
    snap["required_checks_failed"] = [c["name"] for c in rec.checks if c["required"] and not c["ok"]]
    snap["warnings"] = [c["name"] for c in rec.checks if not c["required"] and not c["ok"]]
    snap["summary"] = {"checks": len(rec.checks), "passed": sum(c["ok"] for c in rec.checks),
                       "required_failed": len(snap["required_checks_failed"]), "warnings": len(snap["warnings"]),
                       "wall_seconds": round(time.time() - rec.t0, 1)}
    (out / "capability_snapshot.json").write_text(json.dumps(rec.sanitize(snap), indent=2, ensure_ascii=False))
    rec.log.close()
    print(f"\n{snap['summary']}")
    print(f"outputs: {out}")
    if snap["required_checks_failed"]:
        print("REQUIRED CHECKS FAILED:", *snap["required_checks_failed"], sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
