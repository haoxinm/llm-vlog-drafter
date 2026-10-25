#!/usr/bin/env python3
"""A small stateful stand-in for ``filmcraft-cli mcp`` used by the adapter's unit tests.

Speaks newline-delimited JSON-RPC on stdio exactly like the real server (initialize, tools/list, tools/call)
and implements just the engine commands the adapter allowlists, with the response shapes recorded from
FilmCraft 0.13.0 in Phase 0. Like the real engine it does NOT validate source ranges or track occupancy.

  fake_filmcraft.py --version
  fake_filmcraft.py mcp [--project p.fcproj] [--data-dir d]

Behaviour switches (environment variables):
  FAKE_FC_FAIL_ON=<command id>      that command returns isError
  FAKE_FC_FAIL_AFTER=<n>            ...but only after n successful calls of it
  FAKE_FC_LOSE_CLIPS_ON_SAVE=1      saved projects lose their clips (persistence failure)
  FAKE_FC_SLEEP_ON=<command id>     that command sleeps 30 s (timeout tests)
  FAKE_FC_NO_AUDIO=<basename,...>   movies without an audio stream
  FAKE_FC_COUNT_FILE=<path>         append one line per server start (to count processes)
  FAKE_FC_OMIT_COMMAND=<id>         leave a command out of command_list
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
TPS = 254_016_000_000
VERSION = "0.13.0-fake"


def out(msg):
    sys.stdout.buffer.write((json.dumps(msg) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


class Fail(Exception):
    pass


class Engine:
    def __init__(self):
        self.next_id = 1
        self.items = {}  # id -> dict
        self.sequences = {}  # id -> dict
        self.active = None
        self.project_name = "Untitled"
        self.path = None
        self.dirty = False
        self.calls = {}

    def nid(self):
        i = self.next_id
        self.next_id += 1
        return i

    # ---- persistence
    def dump(self):
        return {"format": "fake.project", "name": self.project_name, "next_id": self.next_id, "items": self.items,
                "sequences": self.sequences, "active": self.active}

    def load(self, p):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        self.project_name, self.next_id, self.active = d["name"], d["next_id"], d["active"]
        self.items = {int(k): v for k, v in d["items"].items()}
        self.sequences = {int(k): v for k, v in d["sequences"].items()}
        self.path = p

    def save(self, p):
        d = self.dump()
        if os.environ.get("FAKE_FC_LOSE_CLIPS_ON_SAVE"):
            for s in d["sequences"].values():
                for t in s["tracks"]:
                    t["clips"] = []
        Path(p).write_text(json.dumps(d), encoding="utf-8")
        self.path = p
        self.dirty = False
        return {"backup": None, "bytes": os.path.getsize(p), "ms": 1.0, "path": p}

    @property
    def seq(self):
        if self.active is None:
            raise Fail("no active sequence")
        return self.sequences[self.active]

    def fps(self):
        return Fraction(self.seq["fps"][0], self.seq["fps"][1])

    def frame_ticks(self, frames):
        return int(frames * Fraction(TPS) / self.fps())

    def track(self, ref, kind=None):
        s = self.seq
        k = ref[0]
        idx = int(ref[1:]) - 1
        ts = [t for t in s["tracks"] if t["kind"] == ("video" if k == "V" else "audio")]
        if idx >= len(ts):
            raise Fail(f"no track {ref}")
        return ts[idx]

    # ---- commands
    def run(self, cid, p):
        self.calls[cid] = self.calls.get(cid, 0) + 1
        if os.environ.get("FAKE_FC_SLEEP_ON") == cid:
            time.sleep(30)
        if os.environ.get("FAKE_FC_FAIL_ON") == cid and self.calls[cid] > int(os.environ.get("FAKE_FC_FAIL_AFTER", "0")):
            raise Fail("injected failure")
        fn = getattr(self, "c_" + cid.replace(".", "_"), None)
        if fn is None:
            raise Fail(f"unknown command {cid}")
        self.dirty = True
        return fn(p)

    def c_file_newProject(self, p):
        self.__init__()
        self.project_name = p["name"]
        return None

    def c_file_saveAs(self, p):
        return self.save(p["path"])

    def c_file_save(self, p):
        path = p.get("path") or self.path
        if not path:
            raise Fail("project has no path")
        return self.save(path)

    def c_file_newSequence(self, p):
        sid = self.nid()
        s = {"id": sid, "name": p["name"], "width": p.get("width", 1920), "height": p.get("height", 1080), "markers": [], "tracks": []}
        f = Fraction(p.get("fps", 23.976)).limit_denominator(1001) if not isinstance(p.get("fps"), int) else Fraction(p["fps"])
        s["fps"] = [f.numerator, f.denominator]
        for kind, n in (("video", p.get("video", 3)), ("audio", p.get("audio", 3))):
            for i in range(n):
                s["tracks"].append({"kind": kind, "id": self.nid(), "name": f"{kind.capitalize()} {i + 1}", "clips": [], "transitions": []})
        self.sequences[sid] = s
        self.items[sid] = {"item": sid, "name": p["name"], "type": "Sequence", "duration": 0}
        self.active = sid
        return {"sequence": sid}

    def c_sequence_addTracks(self, p):
        return {"video": [], "audio": [], "submix": []}

    def c_media_status(self, p):
        return [{"identity": {"fingerprint": "f%d" % i["item"], "size": 1}, "item": i["item"], "name": i["name"], "path": i["path"],
                 "proxy": None, "status": "online" if os.path.exists(i["path"]) else "offline"}
                for i in self.items.values() if "path" in i]

    def c_timeline_place(self, p):
        it = self.items[p["item"]]
        s = self.seq
        fps = self.fps()
        start = p["frame"] if "frame" in p else int(Fraction(p["time"], TPS) * fps) if "time" in p else int(Fraction(p["seconds"]) * fps)
        dur = p.get("duration", it["duration"])
        src_in = p.get("sourceIn", 0)
        made = []
        vt = at = None
        if it["type"] in ("Movie", "Still Image") and p.get("video", True):
            if p["track"][0] != "V":
                raise Fail("video clip needs a V track")
            vt = self.track(p["track"])
        if it["type"] == "Audio":
            at = self.track(p["track"])
        elif it["type"] == "Movie" and p.get("audio", True) and not it.get("silent"):
            at = self.track(p.get("audioTrack") or "A" + p["track"][1:])
        link = None
        for t, kind in ((vt, "video"), (at, "audio")):
            if t is None:
                continue
            cid = self.nid()
            c = {"clip": cid, "item": it["item"], "name": it["name"], "start": self.frame_ticks(start), "startFrame": start,
                 "end": self.frame_ticks(start) + dur, "endFrame": start + round(Fraction(dur) / (Fraction(TPS) / fps)),
                 "duration": dur, "durationFrames": round(Fraction(dur) / (Fraction(TPS) / fps)), "sourceIn": src_in,
                 "sourceOut": src_in + dur, "gainDb": 0.0, "speed": 1.0, "link": None, "enabled": True, "kind": kind}
            t["clips"].append(c)
            made.append(c)
        if len(made) == 2:
            made[0]["link"], made[1]["link"] = made[1]["clip"], made[0]["clip"]
        return {"clips": [c["clip"] for c in made]}

    def find_clip(self, cid):
        for t in self.seq["tracks"]:
            for c in t["clips"]:
                if c["clip"] == cid:
                    return t, c
        raise Fail(f"no clip {cid}")

    def c_clip_audioGain(self, p):
        for cid in p["clips"]:
            t, c = self.find_clip(cid)
            c["gainDb"] = p["db"]
        return {"clips": len(p["clips"]), "gainDb": [{"clip": c, "gainDb": p["db"]} for c in p["clips"]], "mode": "set"}

    def c_sequence_applyVideoTransition(self, p):
        t, c = self.find_clip(p["clip"])
        prev = [x for x in t["clips"] if x["endFrame"] == c["startFrame"]]
        if not prev:
            raise Fail("no adjacent clip")
        tid = self.nid()
        t["transitions"].append({"id": tid, "effect": "cross_dissolve", "align": "center", "duration": self.frame_ticks(p["frames"]),
                                 "start": c["start"] - self.frame_ticks(p["frames"]) // 2, "from": prev[0]["clip"], "to": c["clip"],
                                 "params": {}, "reverse": False})
        return {"transition": tid}

    def c_graphics_newText(self, p):
        t = self.seq["tracks"][p["track"]]
        fps = self.fps()
        start = int(Fraction(p.get("time", 0), TPS) * fps)
        dur = int(round(Fraction(p.get("seconds", 5)) * fps))
        for c in t["clips"]:
            if c["startFrame"] < start + dur and start < c["endFrame"]:
                raise Fail(f"V{p['track'] + 1} is not free here")
        gid = self.nid()
        self.items[gid] = {"item": gid, "name": "Graphic", "type": "Graphic", "duration": 0}
        cid = self.nid()
        t["clips"].append({"clip": cid, "item": gid, "name": p["text"], "start": self.frame_ticks(start), "startFrame": start,
                           "end": self.frame_ticks(start + dur), "endFrame": start + dur, "duration": self.frame_ticks(dur),
                           "durationFrames": dur, "sourceIn": 0, "sourceOut": self.frame_ticks(dur), "gainDb": 0.0, "speed": 1.0,
                           "link": None, "enabled": True, "kind": "video"})
        return {"clip": cid, "layer": 0}

    def c_markers_add(self, p):
        mid = self.nid()
        self.seq["markers"].append({"id": mid, "kind": "Comment", "name": p.get("name", ""), "comment": p.get("comment", ""),
                                    "color": "Green", "start": p["time"], "duration": 0})
        return {"marker": mid}

    def c_file_exportMedia(self, p):
        Path(p["path"]).write_bytes(b"not really an mp4")
        return {"job": 1, "offlineMedia": [], "path": p["path"], "result": {"bytes": 17, "frames": 1, "path": p["path"]}}

    def c_file_exportInterchange(self, p):
        fmt, path = p["format"], Path(p["path"])
        s = self.seq
        media_dir_rel = lambda m: urllib.parse.quote(os.path.relpath(m, path.parent).replace("\\", "/"))
        if fmt == "otio":
            tracks = []
            for t in s["tracks"]:
                kids, pos = [], 0
                for c in sorted(t["clips"], key=lambda c: c["startFrame"]):
                    if c["startFrame"] > pos:
                        kids.append({"OTIO_SCHEMA": "Gap.1", "source_range": {"start_time": {"value": 0}, "duration": {"value": c["startFrame"] - pos}}})
                    item = self.items[c["item"]]
                    ref = ({"OTIO_SCHEMA": "ExternalReference.1", "target_url": media_dir_rel(item["path"])} if "path" in item
                           else {"OTIO_SCHEMA": "MissingReference.1"})
                    kids.append({"OTIO_SCHEMA": "Clip.2", "name": c["name"], "active_media_reference_key": "DEFAULT_MEDIA",
                                 "media_references": {"DEFAULT_MEDIA": ref},
                                 "source_range": {"start_time": {"value": round(c["sourceIn"] / TPS * 30)}, "duration": {"value": c["durationFrames"]}}})
                    pos = c["endFrame"]
                tracks.append({"OTIO_SCHEMA": "Track.1", "name": t["name"], "kind": t["kind"].capitalize(), "children": kids})
            doc = {"OTIO_SCHEMA": "Timeline.1", "name": s["name"], "tracks": {"OTIO_SCHEMA": "Stack.1", "children": tracks, "markers": []}}
            path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            return {"path": str(path), "report": ["graphic clip has no OTIO equivalent (fake)"]}
        if fmt == "xml":
            body = []
            for kind in ("video", "audio"):
                body.append(f"<{kind}>")
                for t in [t for t in s["tracks"] if t["kind"] == kind]:
                    body.append("<track>")
                    for c in sorted(t["clips"], key=lambda c: c["startFrame"]):
                        item = self.items[c["item"]]
                        f = f"<file id=\"f{c['item']}\"><pathurl>file://localhost{urllib.parse.quote(item['path'])}</pathurl></file>" if "path" in item else ""
                        body.append(f"<clipitem><name>{c['name']}</name><start>{c['startFrame']}</start><end>{c['endFrame']}</end>{f}</clipitem>")
                    body.append("</track>")
                body.append(f"</{kind}>")
            dur = max([c["endFrame"] for t in s["tracks"] for c in t["clips"]] or [0])
            path.write_text(f"<?xml version=\"1.0\"?><xmeml version=\"4\"><sequence><duration>{dur}</duration><media>{''.join(body)}</media></sequence></xmeml>", encoding="utf-8")
            return {"path": str(path), "report": []}
        path.write_text(f"fake {fmt}", encoding="utf-8")
        return {"path": str(path), "report": []}

    def c_edit_undo(self, p):
        return {"undone": "x"}

    def c_file_open(self, p):
        self.load(p["path"])
        return None

    # ---- inspection
    def inspect_sequence(self):
        s = self.seq
        out_ = {"id": s["id"], "name": s["name"], "markers": s["markers"], "video": [], "audio": [],
                "settings": {"width": s["width"], "height": s["height"], "frame_rate": {"num": s["fps"][0], "den": s["fps"][1]}}}
        end = 0
        for t in s["tracks"]:
            items = [{k: v for k, v in c.items() if k != "kind"} for c in t["clips"]]
            end = max([end] + [c["endFrame"] for c in t["clips"]])
            out_[t["kind"]].append({"id": t["id"], "name": t["name"], "items": items, "transitions": t["transitions"]})
        out_["durationFrames"] = end
        return out_

    def inspect_project(self):
        kids = [{"item": i["item"], "name": i["name"], "type": i["type"], "duration": i["duration"]} for i in self.items.values()]
        return {"name": self.project_name, "path": self.path, "dirty": self.dirty, "activeSequence": self.active, "revision": 1,
                "root": {"id": 0, "name": self.project_name, "children": kids}}

    def import_media(self, paths):
        durs = json.loads(os.environ.get("FAKE_FC_DURATIONS", "{}"))
        silent = [x for x in os.environ.get("FAKE_FC_NO_AUDIO", "").split(",") if x]
        ids, errors = [], []
        for pth in paths:
            if not os.path.exists(pth):
                errors.append({"path": pth, "error": "not found"})
                continue
            name = os.path.basename(pth)
            ext = name.rsplit(".", 1)[-1].lower()
            typ = "Still Image" if ext in ("png", "jpg", "jpeg") else "Audio" if ext in ("wav", "m4a", "mp3") else "Movie"
            m = re.search(r"_(\d+)s\.", name)  # landscape_5s.mp4 -> 5 s, like the generated fixtures
            secs = durs.get(name, float(m[1]) if m else 5.0)
            i = self.nid()
            self.items[i] = {"item": i, "name": name, "type": typ, "duration": int(Fraction(str(secs)) * TPS), "path": pth,
                             "silent": name in silent}
            ids.append(i)
        return {"errors": errors, "items": ids}


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(f"filmcraft-cli {VERSION}")
        return 0
    if argv[:1] != ["mcp"]:
        print("usage", file=sys.stderr)
        return 2
    if os.environ.get("FAKE_FC_COUNT_FILE"):
        with open(os.environ["FAKE_FC_COUNT_FILE"], "a") as f:
            f.write("start\n")
    eng = Engine()
    if "--project" in argv:
        eng.load(argv[argv.index("--project") + 1])
    tools = json.loads((HERE / "mcp_tools.json").read_text(encoding="utf-8"))
    catalog = json.loads((HERE / "command_catalog_subset.json").read_text(encoding="utf-8"))
    omit = os.environ.get("FAKE_FC_OMIT_COMMAND")
    catalog = [c for c in catalog if c["id"] != omit]
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line.decode("utf-8"))
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue
        try:
            if method == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}},
                       "serverInfo": {"name": "filmcraft", "version": VERSION}, "instructions": "fake"}
            elif method == "tools/list":
                res = {"tools": tools}
            elif method == "tools/call":
                name, args = params["name"], params.get("arguments") or {}
                try:
                    if name == "command_list":
                        payload = catalog
                    elif name == "command_run":
                        payload = eng.run(args["id"], args.get("params") or {})
                    elif name == "sequence_inspect":
                        payload = eng.inspect_sequence()
                    elif name == "project_inspect":
                        payload = eng.inspect_project()
                    elif name == "media_import":
                        payload = eng.import_media(args["paths"])
                    else:
                        raise Fail(f"unknown tool {name}")
                    text = json.dumps(payload, ensure_ascii=False) if payload is not None else "null"
                    res = {"content": [{"type": "text", "text": text}]}
                except Fail as e:
                    res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
                except KeyError as e:
                    res = {"content": [{"type": "text", "text": f"invalid parameters: missing {e}"}], "isError": True}
            else:
                out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
                continue
            out({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:  # noqa: BLE001
            out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": repr(e)}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
