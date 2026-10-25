"""A small stateful fake of DaVinci Resolve's scripting API for ``resolve-finish`` tests (no Resolve needed).

It models what was observed on Resolve Studio 21.1.1.10 with the verifier:

* ``ImportTimelineFromFile`` parses a real FCP7 XML (clips, linked media, transitions as media-less items, markers);
* a still whose file name contains digits is misread as an image sequence (``still_[1080-1229]p.png``,
  ``IMG_[1042-1191].JPG``) and is offline; ``MediaPoolItem.ReplaceClip(<existing file>)`` brings it online with the
  right name and the same record range;
* timelines start at frame ``TL_START``; transitions are listed as items without a media pool item;
* a project name that exists makes ``CreateProject`` return None; a reused timeline name makes the import return None.

Behaviour switches (constructor arguments) let tests simulate the unproven title/marker calls succeeding or failing.
Calls the pipeline must never make (project delete/load/close) raise AssertionError.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

TL_START = 86400
SEQ_RE = re.compile(r"^(.*?)(\d+)(\D*)\.([A-Za-z0-9]+)$")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def _path_from_url(url: str) -> str:
    u = urlparse(url)
    p = unquote(u.path)
    if re.match(r"^/[A-Za-z]:/", p):
        p = p[1:]
    return p


class FakeMPI:
    def __init__(self, path: str | None, name: str | None = None, timeline: Any = None):
        self.path, self.timeline = path, timeline
        self._name = name or (Path(path).name if path else "")
        self.items: list[FakeItem] = []
        self.replace_calls: list[str] = []

    def GetName(self): return self._name
    def GetClipProperty(self, key=None): return self.path if key == "File Path" else None

    def ReplaceClip(self, path):
        self.replace_calls.append(path)
        if not Path(path).is_file():
            return False
        self.path, self._name = path, Path(path).name
        for it in self.items:
            it.name, it.online = Path(path).name, True
        return True


class FakeComp:
    def __init__(self, item: "FakeItem"): self.item = item
    def FindToolByID(self, tool_id):
        return FakeTool(self.item) if tool_id == "TextPlus" and self.item.text_tool_ok else None
    def FindTool(self, name):
        return None


class FakeTool:
    def __init__(self, item): self.item = item
    def SetInput(self, name, value):
        if name == "StyledText" and self.item.accept_text:
            self.item.text = value
            return True
        return False
    def GetInput(self, name): return self.item.text if name == "StyledText" else None
    def GetAttrs(self): return {"TOOLS_RegID": "TextPlus"}


class FakeItem:
    def __init__(self, name, start, end, left, mpi, online=True, tl_start=TL_START):
        self.name, self.s, self.e, self.left, self.mpi, self.online, self.tl_start = name, start, end, left, mpi, online, tl_start
        self.fusion_comps = 0
        self.text = None
        self.text_tool_ok = True
        self.accept_text = True
        if mpi is not None:
            mpi.items.append(self)

    def GetName(self): return self.name
    def GetStart(self): return self.s + self.tl_start
    def GetEnd(self): return self.e + self.tl_start
    def GetDuration(self): return self.e - self.s
    def GetLeftOffset(self): return self.left
    def GetMediaPoolItem(self): return self.mpi
    def GetFusionCompCount(self): return self.fusion_comps
    def GetFusionCompByIndex(self, i): return FakeComp(self) if 1 <= i <= self.fusion_comps else None


class FakeTimeline:
    def __init__(self, name, project: "FakeProject", tracks=None, dur=0):
        self.name, self.project = name, project
        self.tracks: dict[str, list[list[FakeItem]]] = tracks or {"video": [[]], "audio": [[]]}
        self.markers: dict[int, dict] = {}
        self.dur = dur
        self.settings = {"timelineFrameRate": project.settings.get("timelineFrameRate", "24"),
                         "timelineResolutionWidth": project.settings.get("timelineResolutionWidth", "1920"),
                         "timelineResolutionHeight": project.settings.get("timelineResolutionHeight", "1080")}
        self.add_marker_calls: list[tuple] = []

    def GetName(self): return self.name
    def GetStartFrame(self): return TL_START
    def GetEndFrame(self):
        ends = [i.e for t in self.tracks.values() for tr in t for i in tr]
        return TL_START + max(ends + [self.dur])
    def GetSetting(self, k): return self.settings.get(k)
    def GetTrackCount(self, kind): return len(self.tracks.get(kind, []))
    def GetTrackName(self, kind, i): return f"{kind} {i}"
    def GetItemListInTrack(self, kind, i): return list(self.tracks[kind][i - 1])
    def GetMarkers(self): return dict(self.markers)
    def AddMarker(self, frame, color, name, note, duration, custom):
        self.add_marker_calls.append((frame, color, name, note, duration, custom))
        if frame in self.markers or self.project.resolve.reject_markers:
            return False
        self.markers[frame] = {"name": name, "color": color, "note": note, "duration": duration}
        return True
    def AddTrack(self, kind, sub=None):
        if self.project.resolve.reject_add_track:
            return False
        self.tracks[kind].append([])
        return True

    def _insert_title(self, tname):
        r = self.project.resolve
        if r.titles == "no_insert":
            return None
        it = FakeItem("Text+", 0, r.title_default_frames, 0, FakeMPI(None, "Text+"))
        it.fusion_comps = 0 if r.titles == "no_comp" else 1
        it.text_tool_ok = r.titles != "no_tool"
        it.accept_text = r.titles != "reject_text"
        self.tracks["video"][0].append(it)
        return it

    def InsertFusionTitleIntoTimeline(self, name): return self._insert_title(name)
    def InsertTitleIntoTimeline(self, name): return self._insert_title(name) if self.project.resolve.titles != "fusion_only" else None

    def DeleteClips(self, items, ripple=False):
        for it in items:
            for t in self.tracks.values():
                for tr in t:
                    if it in tr:
                        tr.remove(it)
        return True


class FakeFolder:
    def __init__(self, project): self.project = project
    def GetName(self): return "Master"
    def GetClipList(self): return [t.mpi for t in self.project.timelines if getattr(t, "mpi", None) is not None]


class FakeMediaPool:
    def __init__(self, project): self.project = project

    def GetRootFolder(self): return FakeFolder(self.project)

    def CreateEmptyTimeline(self, name):
        p = self.project
        if any(t.name == name for t in p.timelines):
            return None
        tl = FakeTimeline(name, p)
        tl.mpi = FakeMPI(None, name, tl)
        p.timelines.append(tl)
        p.current = tl
        return tl

    def DeleteTimelines(self, tls):
        for t in tls:
            self.project.timelines.remove(t)
        return True

    def AppendToTimeline(self, infos):
        p, r, out = self.project, self.project.resolve, []
        tl = p.current
        for info in infos:
            mpi = info["mediaPoolItem"]
            # Resolve takes recordFrame as an absolute timeline frame; the switch simulates it being used as given.
            rec = info["recordFrame"] + r.append_shift - (0 if r.append_record_relative else TL_START)
            if "endFrame" in info:  # 0-based frames of the clip
                dur = info["endFrame"] - info["startFrame"] + 1
            else:  # the whole clip: a timeline media pool item is as long as its timeline
                dur = mpi.timeline.GetEndFrame() - mpi.timeline.GetStartFrame()
            ti = info["trackIndex"]
            while len(tl.tracks["video"]) < ti:
                tl.tracks["video"].append([])
            it = FakeItem(mpi.GetName(), rec, rec + dur, info.get("startFrame", 0), mpi)
            tl.tracks["video"][ti - 1].append(it)
            out.append(it)
        return out

    def ImportTimelineFromFile(self, path, opts=None):
        p, r = self.project, self.project.resolve
        r.imports.append((path, opts))
        if r.import_returns_none or not Path(path).is_file():
            return None
        name = (opts or {}).get("timelineName")
        if name and any(t.name == name for t in p.timelines):
            return None
        tl = parse_xml_timeline(Path(path), p, name or "Imported", r)
        p.timelines.append(tl)
        tl.mpi = FakeMPI(None, tl.name, tl)
        p.current = tl
        return tl


class FakeProject:
    def __init__(self, name, resolve):
        self.name, self.resolve = name, resolve
        self.settings: dict[str, str] = {}
        self.timelines: list[FakeTimeline] = []
        self.current: FakeTimeline | None = None
        self.mp = FakeMediaPool(self)
        self.set_setting_calls: list[tuple] = []

    def GetName(self): return self.name
    def SetSetting(self, k, v):
        self.set_setting_calls.append((k, v, bool(self.timelines)))
        self.settings[k] = v
        return True
    def GetSetting(self, k): return self.settings.get(k)
    def GetMediaPool(self): return self.mp
    def SetCurrentTimeline(self, tl): self.current = tl; return True
    def GetCurrentTimeline(self): return self.current


class FakePM:
    def __init__(self, resolve, existing):
        self.resolve = resolve
        self.projects: dict[str, FakeProject] = {n: FakeProject(n, resolve) for n in existing}
        self.cur: str | None = next(iter(self.projects), None)
        self.saved: list[str] = []
        self.created: list[str] = []

    def GetCurrentProject(self): return self.projects.get(self.cur) if self.cur else None
    def GetCurrentFolder(self): return "root"
    def GetProjectListInCurrentFolder(self): return list(self.projects)
    def CreateProject(self, name):
        if name in self.projects:
            return None
        p = FakeProject(name, self.resolve)
        self.projects[name] = p
        self.cur = name
        self.created.append(name)
        return p
    def SaveProject(self):
        self.saved.append(self.cur)
        return True
    # never allowed: guard must keep these unreachable
    def DeleteProject(self, n): raise AssertionError("DeleteProject must never be called")
    def LoadProject(self, n): raise AssertionError("LoadProject must never be called")
    def CloseProject(self, p): raise AssertionError("CloseProject must never be called")


class FakeResolve:
    def __init__(self, *, product="DaVinci Resolve Studio", version="21.1.1.10", existing_projects=("My Trip",),
                 misread_stills=True, drop_markers=False, import_returns_none=False, reject_markers=False,
                 titles="ok", title_default_frames=150, append_record_relative=False, append_shift=0,
                 reject_add_track=False, offline_all=False):
        self.product, self.version = product, version
        self.misread_stills, self.drop_markers = misread_stills, drop_markers
        self.import_returns_none, self.reject_markers = import_returns_none, reject_markers
        self.titles, self.title_default_frames = titles, title_default_frames
        self.append_record_relative, self.append_shift = append_record_relative, append_shift
        self.reject_add_track, self.offline_all = reject_add_track, offline_all
        self.imports: list[tuple] = []
        self.pm = FakePM(self, existing_projects)

    def GetProjectManager(self): return self.pm
    def GetProductName(self): return self.product
    def GetVersionString(self): return self.version
    def GetVersion(self): return [21, 1, 1, 10, ""]
    def Quit(self): raise AssertionError("Quit must never be called")


def _t(el, tag, default=None):
    c = el.find(tag)
    return c.text if c is not None and c.text is not None else default


def parse_xml_timeline(path: Path, project: FakeProject, name: str, r: FakeResolve) -> FakeTimeline:
    root = ET.parse(path).getroot()
    seq = root.find("sequence")
    files: dict[str, str] = {}
    mpis: dict[str, FakeMPI] = {}
    tracks: dict[str, list[list[FakeItem]]] = {"video": [], "audio": []}
    for kind in ("video", "audio"):
        for tr in seq.findall(f"media/{kind}/track"):
            items: list[FakeItem] = []
            for el in tr:
                if el.tag == "clipitem":
                    f = el.find("file")
                    fid = f.get("id") if f is not None else None
                    url = _t(f, "pathurl") if f is not None else None
                    if url:
                        files[fid] = _path_from_url(url)
                    fpath = files.get(fid)
                    start, end = int(_t(el, "start")), int(_t(el, "end"))
                    s_in = int(_t(el, "in", 0))
                    key = fpath
                    if key not in mpis:
                        mpis[key] = _make_mpi(fpath, r)
                    mpi = mpis[key]
                    items.append(FakeItem(mpi._name, start, end, s_in, mpi))
                elif el.tag == "transitionitem":
                    nm = "Cross Dissolve" if kind == "video" else "Final Cut Pro 7"
                    items.append(FakeItem(nm, int(_t(el, "start")), int(_t(el, "end")), None, None))
            tracks[kind].append(items)
    dur = int(_t(seq, "duration", 0))
    tl = FakeTimeline(name, project, tracks, dur)
    if not r.drop_markers:
        for m in seq.findall("marker"):
            tl.markers[int(_t(m, "in"))] = {"name": _t(m, "name", ""), "color": "Blue"}
    return tl


def _make_mpi(fpath: str, r: FakeResolve) -> FakeMPI:
    p = Path(fpath)
    m = SEQ_RE.match(p.name)
    if r.misread_stills and p.suffix.lower() in IMAGE_EXTS and m:
        lo = int(m.group(2))
        seq_name = f"{m.group(1)}[{lo}-{lo + 149}]{m.group(3)}.{m.group(4)}"
        mpi = FakeMPI(str(p.with_name(seq_name)), seq_name)
        mpi.misread = True
        return mpi
    mpi = FakeMPI(fpath + ".offline" if r.offline_all else fpath)
    mpi.misread = False
    return mpi
