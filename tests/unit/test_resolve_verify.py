"""Tests for tools/resolve_verify.py using a fake Resolve object model (no Resolve needed)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("resolve_verify", ROOT / "tools" / "resolve_verify.py")
rv = importlib.util.module_from_spec(_spec)
sys.modules["resolve_verify"] = rv
_spec.loader.exec_module(rv)


# ---------------------------------------------------------------- fake Resolve
class FakeMPI:
    def __init__(self, path):
        self.path = path

    def GetClipProperty(self, key=None):
        return self.path if key == "File Path" else None


class FakeItem:
    def __init__(self, name, start, end, left, path, tl_start=86400, online=True):
        self.name, self.s, self.e, self.left, self.path, self.tl_start, self.online = name, start, end, left, path, tl_start, online

    def GetName(self): return self.name
    def GetStart(self): return self.s + self.tl_start
    def GetEnd(self): return self.e + self.tl_start
    def GetDuration(self): return self.e - self.s
    def GetLeftOffset(self): return self.left
    def GetMediaPoolItem(self): return FakeMPI(self.path) if self.path and self.online else None


class FakeTimeline:
    def __init__(self, items, fps="30", w="1920", h="1080", dur=195, markers=None, tl_start=86400):
        self.items, self.settings = items, {"timelineFrameRate": fps, "timelineResolutionWidth": w, "timelineResolutionHeight": h}
        self.dur, self.markers, self.tl_start = dur, markers or {}, tl_start

    def GetName(self): return "verify"
    def GetStartFrame(self): return self.tl_start
    def GetEndFrame(self): return self.tl_start + self.dur
    def GetSetting(self, k): return self.settings.get(k)
    def GetTrackCount(self, kind): return len(self.items.get(kind, []))
    def GetItemListInTrack(self, kind, i): return self.items[kind][i - 1]
    def GetMarkers(self): return self.markers


class FakeProject:
    def __init__(self, name, timeline, none_with_options=False):
        self.name, self.timeline, self.settings = name, timeline, {}
        self.imports: list[tuple] = []
        self.none_with_options = none_with_options
        self.seen_names: set[str] = set()

    def GetName(self): return self.name
    def SetSetting(self, k, v): self.settings[k] = v; return True
    def GetMediaPool(self): return self

    def ImportTimelineFromFile(self, path, opts=None):
        self.imports.append((path, opts))
        if self.none_with_options and opts is not None:
            return None
        if opts and opts.get("timelineName") in self.seen_names:
            return None  # Resolve silently returns None for a reused timeline name
        if opts:
            self.seen_names.add(opts["timelineName"])
        return self.timeline

    def SetCurrentTimeline(self, tl): return True


class FakePM:
    """Models: the loaded project cannot be deleted; DeleteProject only sees the current folder."""
    def __init__(self, timeline, prev="UserProject", folder="Sub", none_with_options=False):
        self.timeline, self.none_with_options = timeline, none_with_options
        self.folders = {"root": {}, "Sub": {}}
        self.cur_folder = folder
        self.prev = prev
        if prev:
            self.folders[folder][prev] = FakeProject(prev, None)
        self.cur = prev
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.created: FakeProject | None = None

    def GetCurrentProject(self):
        return self.folders[self.cur_folder].get(self.cur) if self.cur else FakeProject("Untitled Project", None)
    def GetCurrentFolder(self): return self.cur_folder
    def GotoRootFolder(self): self.calls.append("GotoRootFolder"); self.cur_folder = "root"; return True
    def OpenFolder(self, n):
        self.calls.append(f"OpenFolder({n})")
        if n in self.folders: self.cur_folder = n; return True
        return False
    def GetProjectListInCurrentFolder(self): return list(self.folders[self.cur_folder])
    def CreateProject(self, n):
        p = FakeProject(n, self.timeline, self.none_with_options)
        self.folders[self.cur_folder][n] = p; self.cur = n; self.created = p; return p
    def CloseProject(self, p): self.calls.append("CloseProject"); return True  # stays "current" until another loads
    def LoadProject(self, n):
        self.calls.append(f"LoadProject({n})")
        if n in self.folders[self.cur_folder]: self.cur = n; return self.folders[self.cur_folder][n]
        return None
    def DeleteProject(self, n):
        self.calls.append(f"DeleteProject({n})")
        if n == self.prev: raise AssertionError("must never delete user project")
        if n == self.cur or n not in self.folders[self.cur_folder]: return False
        self.deleted.append(n); self.folders[self.cur_folder].pop(n); return True


class FakeResolve:
    def __init__(self, timeline, **kw): self.pm = FakePM(timeline, **kw)
    def GetProjectManager(self): return self.pm
    def GetVersionString(self): return "19.1.0"
    def GetProductName(self): return "DaVinci Resolve Studio"


# ---------------------------------------------------------------- fixtures
def make_expected(media_dir: Path) -> dict:
    a, b = str(media_dir / "a.mp4"), str(media_dir / "b.png")
    return {
        "fps": 30, "width": 1920, "height": 1080, "duration_frames": 195, "transitions": 1,
        "markers": [{"name": "Scene A", "frame": 60}],
        "tracks": [
            {"name": "Video 1", "kind": "video", "clips": [
                {"name": "a.mp4", "source_path": a, "record_start_frame": 0, "record_end_frame": 105, "source_in_frame": 15, "source_out_frame": 120},
                {"name": "b.png", "source_path": b, "record_start_frame": 105, "record_end_frame": 195, "source_in_frame": 0, "source_out_frame": 90}]},
            {"name": "Video 2", "kind": "video", "clips": [
                {"name": "Hello Title", "source_path": None, "record_start_frame": 0, "record_end_frame": 60, "source_in_frame": 0, "source_out_frame": 60}]},
            {"name": "Audio 1", "kind": "audio", "clips": [
                {"name": "a.mp4", "source_path": a, "record_start_frame": 0, "record_end_frame": 105, "source_in_frame": 15, "source_out_frame": 120}]},
        ],
    }


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"; d.mkdir()
    (d / "a.mp4").write_bytes(b"x"); (d / "b.png").write_bytes(b"x")
    return d


def make_actual_timeline(media: Path, **kw) -> FakeTimeline:
    a, b = str(media / "a.mp4"), str(media / "b.png")
    items = {"video": [[FakeItem("a.mp4", 0, 105, 15, a), FakeItem("b.png", 105, 195, 0, b)],
                       [FakeItem("Hello Title", 0, 60, 0, None)]],
             "audio": [[FakeItem("a.mp4", 0, 105, 15, a)]]}
    return FakeTimeline(items, markers={60 + 86400 - 86400: {"name": "Scene A", "color": "Blue"}}, **kw)


def checks(res):
    return {c["check"]: c for c in res}


# ---------------------------------------------------------------- discovery
def test_default_paths_per_os():
    api, lib = rv.default_script_paths("darwin")
    assert api == "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    assert lib.endswith("DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
    api, lib = rv.default_script_paths("win32", {"PROGRAMDATA": r"D:\PD"})
    assert api == r"D:\PD\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
    assert lib == r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    api, lib = rv.default_script_paths("linux")
    assert api.startswith("/opt/resolve/") and lib.endswith("fusionscript.so")


def test_env_overrides_defaults(tmp_path):
    api = tmp_path / "Scripting"; (api / "Modules").mkdir(parents=True)
    lib = tmp_path / "fusionscript.so"; lib.write_text("x")
    info = rv.discover_resolve("darwin", {"RESOLVE_SCRIPT_API": str(api), "RESOLVE_SCRIPT_LIB": str(lib)})
    assert info["api_source"] == info["lib_source"] == "env"
    assert info["modules"] == str(api / "Modules") and info["modules_exists"] and info["lib_exists"]
    info = rv.discover_resolve("darwin", {})
    assert info["api_source"] == "default" and not info["modules_exists"]


def test_manual_required_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "connect_resolve", lambda *a, **k: (None, {"diagnosis": ["Resolve is not running"]}))
    media_dir = tmp_path / "m"; media_dir.mkdir()
    exp = tmp_path / "expected_timeline.json"; exp.write_text(json.dumps(make_expected(media_dir)))
    tl = tmp_path / "t.xml"; tl.write_text("<xmeml/>")
    out = tmp_path / "res.json"
    code = rv.main(["--timeline", str(tl), "--expected", str(exp), "--output", str(out)])
    assert code == 3
    rep = json.loads(out.read_text())
    assert rep["status"] == "manual_required" and "not running" in rep["diagnosis"][0]
    assert "Manual Resolve import checklist" in capsys.readouterr().out


def test_usage_errors(tmp_path):
    assert rv.main([]) == 2
    tl = tmp_path / "t.xml"; tl.write_text("<x/>")
    assert rv.main(["--timeline", str(tl)]) == 2  # no expected derivable from xml


# ---------------------------------------------------------------- OTIO reader
def rt(v, r=30.0): return {"OTIO_SCHEMA": "RationalTime.1", "rate": r, "value": v}
def tr(s, d, r=30.0): return {"OTIO_SCHEMA": "TimeRange.1", "start_time": rt(s, r), "duration": rt(d, r)}


def clip(name, s, d, url=None):
    mr = ({"OTIO_SCHEMA": "ExternalReference.1", "target_url": url} if url else {"OTIO_SCHEMA": "MissingReference.1"})
    return {"OTIO_SCHEMA": "Clip.2", "name": name, "source_range": tr(s, d),
            "active_media_reference_key": "DEFAULT_MEDIA", "media_references": {"DEFAULT_MEDIA": mr}}


def synthetic_otio():
    return {"OTIO_SCHEMA": "Timeline.1", "name": "Main", "global_start_time": rt(0),
            "metadata": {"filmcraft": {"settings": {"width": 1280, "height": 720, "frame_rate": {"num": 30, "den": 1}}}},
            "tracks": {"OTIO_SCHEMA": "Stack.1", "markers": [{"OTIO_SCHEMA": "Marker.2", "name": "M", "marked_range": tr(60, 0)}],
                       "children": [
                           {"OTIO_SCHEMA": "Track.1", "name": "V1", "kind": "Video", "children": [
                               clip("a.mp4", 15, 105, "../media/a.mp4"),
                               {"OTIO_SCHEMA": "Transition.1", "name": "dissolve", "in_offset": rt(8), "out_offset": rt(7)},
                               {"OTIO_SCHEMA": "Gap.1", "source_range": tr(0, 10)},
                               clip("b.png", 0, 90, "file:///C:/Users/me/My%20Media/b.png")]},
                           {"OTIO_SCHEMA": "Track.1", "name": "V2", "kind": "Video", "children": [clip("Title", 0, 60)]},
                           {"OTIO_SCHEMA": "Track.1", "name": "A1", "kind": "Audio", "children": [clip("a.mp4", 15, 105, "../media/a.mp4")]}]}}


def test_otio_reader_synthetic(tmp_path):
    d = tmp_path / "out"; d.mkdir()
    p = d / "x.otio"; p.write_text(json.dumps(synthetic_otio()))
    e = rv.read_otio_expected(p)
    assert (e["fps"], e["width"], e["height"], e["transitions"]) == (30, 1280, 720, 1)
    v1 = e["tracks"][0]["clips"]
    assert [(c["record_start_frame"], c["record_end_frame"]) for c in v1] == [(0, 105), (115, 205)]  # gap counted, transition not
    assert v1[0]["source_in_frame"] == 15 and v1[0]["source_out_frame"] == 120
    assert v1[0]["source_path"] == rv.normalize_path(str(tmp_path / "media" / "a.mp4"), windows=False)
    assert v1[1]["source_path"] == "C:/Users/me/My Media/b.png"
    assert e["tracks"][1]["clips"][0]["source_path"] is None
    assert e["tracks"][2]["kind"] == "audio" and e["duration_frames"] == 205
    assert e["markers"] == [{"name": "M", "frame": 60}]


def test_otio_reader_rate_conversion():
    doc = synthetic_otio()
    doc["metadata"] = {}
    doc["tracks"]["children"] = [{"OTIO_SCHEMA": "Track.1", "kind": "Video", "children": [
        {**clip("a", 0, 48, "/m/a.mp4"), "source_range": tr(24, 48, 24.0)}]}]
    doc["global_start_time"] = rt(0, 12.0)
    e = rv.otio_to_expected(doc)
    assert e["fps"] == 12.0 and e["tracks"][0]["clips"][0]["record_end_frame"] == 24


def test_otio_reader_real_spike_file():
    p = ROOT / "spike" / "out" / "interchange" / "phase0.otio"
    if not p.is_file():
        pytest.skip("spike output not present")
    e = rv.read_otio_expected(p)
    assert e["fps"] == 30 and (e["width"], e["height"]) == (1920, 1080) and e["duration_frames"] == 195
    assert e["transitions"] == 2 and e["markers"][0]["frame"] == 60
    assert [t["kind"] for t in e["tracks"]] == ["video", "video", "audio", "audio"]
    v1 = e["tracks"][0]["clips"]
    assert [(c["record_start_frame"], c["record_end_frame"], c["source_in_frame"]) for c in v1] == [(0, 105, 15), (105, 195, 0)]
    assert v1[0]["source_path"].endswith("tests/fixtures/generated/landscape_5s.mp4")
    assert e["tracks"][1]["clips"][0]["source_path"] is None  # title


def test_load_expected_json_normalizes(tmp_path):
    p = tmp_path / "e.json"; p.write_text(json.dumps({"fps": 30, "tracks": [{"kind": "video", "clips": [{"name": "x", "source_path": "file:///C:/A/x.mp4"}]}]}))
    e = rv.load_expected_json(p)
    assert e["tracks"][0]["clips"][0]["source_path"] == "C:/A/x.mp4"
    p.write_text("{}")
    with pytest.raises(ValueError):
        rv.load_expected_json(p)


# ---------------------------------------------------------------- paths
@pytest.mark.parametrize("a,b", [
    (r"C:\Users\Me\Clips\a.mp4", "c:/users/me/clips/a.mp4"),
    ("file:///C:/Users/Me/Clips/a.MP4", r"c:\Users\me\clips\a.mp4"),
    ("file://localhost/C:/Users/Me/Clips/a.mp4", r"C:\Users\Me\Clips\a.mp4"),
    (r"\\?\C:\Users\Me\Clips\a.mp4", "C:/Users/Me/Clips/a.mp4"),
    ("file:///C:/My%20Clips/a.mp4", r"C:\My Clips\a.mp4"),
    ("file:///Users/me/My%20Clips/a.mp4", "/Users/Me/my clips/a.mp4"),
])
def test_paths_equal_case_insensitive(a, b):
    assert rv.paths_equal(a, b, fold_case=True)


def test_paths_case_sensitive_and_relative(tmp_path):
    assert not rv.paths_equal("/a/B.mp4", "/a/b.mp4", fold_case=False)
    assert rv.paths_equal("../m/a.mp4", "/x/m/a.mp4", base_a="/x/out", fold_case=False)
    assert rv.normalize_path("C:\\A\\x.mp4", windows=True) == "c:/a/x.mp4"
    assert rv.normalize_path("//srv/share/dir/x.mp4") == "//srv/share/dir/x.mp4"
    assert rv.normalize_path("file://srv/share/x.mp4") == "//srv/share/x.mp4"
    assert rv.normalize_path(None) == ""


# ---------------------------------------------------------------- comparison
def run_compare(media, tl=None, expected=None):
    tl = tl or make_actual_timeline(media)
    return checks(rv.compare_timeline(expected or make_expected(media), rv.dump_timeline(tl), fold_case=False)), rv.compare_timeline


def test_compare_all_pass(media):
    c, _ = run_compare(media)
    for k in ("fps", "resolution", "duration", "track_structure", "clip_order", "record_inout", "source_in", "media_path", "media_online", "markers"):
        assert c[k]["status"] == "pass", (k, c[k])
    assert c["transitions"]["status"] == "info" and "Title" in c["titles"]["detail"] and "present" in c["titles"]["detail"]
    assert rv.format_passed(list(c.values()) + [rv._r("import", "pass")])


def test_tolerance_one_frame_ok_two_fails(media):
    a = str(media / "a.mp4"); b = str(media / "b.png")
    tl = make_actual_timeline(media, dur=196)
    tl.items["video"][0][0].e += 1  # end off by one -> ok
    c, _ = run_compare(media, tl)
    assert c["duration"]["status"] == "pass" and c["record_inout"]["status"] == "pass"
    tl = make_actual_timeline(media, dur=198)
    tl.items["video"][0][1].s += 2
    tl.items["video"][0][0].left = 18
    c, _ = run_compare(media, tl)
    assert c["duration"]["status"] == "fail" and c["record_inout"]["status"] == "fail" and c["source_in"]["status"] == "fail"
    assert not rv.format_passed(list(c.values()) + [rv._r("import", "pass")])


def test_fps_and_resolution_fail(media):
    c, _ = run_compare(media, make_actual_timeline(media, fps="24", w="1280", h="720"))
    assert c["fps"]["status"] == "fail" and c["resolution"]["status"] == "fail"
    c, _ = run_compare(media, make_actual_timeline(media, fps="29.97"))
    assert c["fps"]["status"] == "fail"


def test_order_swapped_fails(media):
    tl = make_actual_timeline(media)
    v = tl.items["video"][0]
    v[0].name, v[1].name, v[0].path, v[1].path = v[1].name, v[0].name, v[1].path, v[0].path
    c, _ = run_compare(media, tl)
    assert c["clip_order"]["status"] == "fail"


def test_offline_media_and_path_mismatch(media):
    tl = make_actual_timeline(media)
    tl.items["video"][0][0].online = False  # no media pool item
    tl.items["video"][0][1].path = str(media / "missing.png")  # not on disk, wrong name
    c, _ = run_compare(media, tl)
    assert c["media_online"]["status"] == "fail" and c["media_path"]["status"] == "fail"
    assert "offline" in c["media_online"]["detail"]


def test_marker_and_track_structure_fail(media):
    tl = make_actual_timeline(media)
    tl.markers = {70: {"name": "Scene A"}}
    tl.items["audio"] = []
    c, _ = run_compare(media, tl)
    assert c["markers"]["status"] == "fail" and c["track_structure"]["status"] == "fail" and c["clip_order"]["status"] == "fail"


def test_title_absent_is_info_only(media):
    tl = make_actual_timeline(media)
    tl.items["video"][1] = []
    c, _ = run_compare(media, tl)
    assert c["titles"]["status"] == "info" and "absent" in c["titles"]["detail"]
    assert c["clip_order"]["status"] == "pass"


def test_windows_path_case_via_fake(media):
    exp = make_expected(media)
    for t in exp["tracks"]:
        for cl in t["clips"]:
            if cl["source_path"]:
                cl["source_path"] = cl["source_path"].upper()
    tl = make_actual_timeline(media)
    res = checks(rv.compare_timeline(exp, rv.dump_timeline(tl), fold_case=True))
    assert res["media_path"]["status"] == "pass"
    res = checks(rv.compare_timeline(exp, rv.dump_timeline(tl), fold_case=False))
    assert res["media_path"]["status"] == "fail"


def test_dump_is_defensive():
    class Broken:
        def GetStartFrame(self): raise RuntimeError("boom")
        def GetEndFrame(self): return None
        def GetSetting(self, k): return None
        def GetTrackCount(self, k): return None
        def GetMarkers(self): return None
    d = rv.dump_timeline(Broken())
    assert d["fps"] is None and d["tracks"] == [] and d["errors"]


# ---------------------------------------------------------------- end to end with fake Resolve
def write_otio(d: Path, doc=None, name="phase0.otio") -> Path:
    p = d / name
    p.write_text(json.dumps(doc if doc is not None else {}))
    return p


def test_run_import_end_to_end_and_cleanup(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media))
    f = write_otio(tmp_path)
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False, work_dir=tmp_path / "w")
    assert res["formats"]["otio[original]"]["passed"]
    pm = resolve.pm
    assert pm.deleted == [res["scratch_project"]] and pm.cur == "UserProject" and pm.cur_folder == "Sub"
    assert "UserProject" in pm.folders["Sub"]
    assert res["scratch_deleted"] is True and res["cleanup_steps"]
    table = rv.markdown_table(res["formats"])
    assert "| clip_order |" in table and "PASS" in table


def test_absolute_paths_options_and_diagnostics(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    x = tmp_path / "rel" / "t.xml"; x.write_text("<xmeml/>")
    resolve = FakeResolve(make_actual_timeline(media))
    res = rv.run_import(resolve, [Path("rel/t.xml")], make_expected(media), keep_project=True, stamp="S1")
    path, opts = resolve.pm.created.imports[0]
    assert Path(path).is_absolute() and path == str(x.resolve())
    assert opts["importSourceClips"] is True and opts["sourceClipsPath"] == str(media) and Path(opts["sourceClipsPath"]).is_absolute()
    e = res["formats"]["fcp7xml"]
    assert e["attempts"] == [{"options": opts, "result": "FakeTimeline"}]
    d = e["diagnostics"]
    assert d["path_passed"] == path and d["exists"] is True and d["size_bytes"] == 8 and d["resolve_return_type"] == "FakeTimeline"


def test_source_clips_dir_common_parent():
    e = {"tracks": [{"clips": [{"source_path": "C:/Users/me/a/x.mp4"}, {"source_path": "C:/Users/me/b/y.mp4"}, {"source_path": None}]}]}
    assert rv.source_clips_dir(e) == r"C:\Users\me"
    e = {"tracks": [{"clips": [{"source_path": "/m/a/x.mp4"}, {"source_path": "/m/a/y.mp4"}]}]}
    assert rv.source_clips_dir(e) == "/m/a"
    assert rv.source_clips_dir({"tracks": []}) is None


def test_unique_timeline_names_per_run_and_label(media, tmp_path):
    f = write_otio(tmp_path, relative_otio())
    names = []
    for stamp in ("A", "B"):
        resolve = FakeResolve(make_actual_timeline(media))
        res = rv.run_import(resolve, [f], make_expected(media), keep_project=True, stamp=stamp, work_dir=tmp_path / "w")
        names += [i[1]["timelineName"] for i in resolve.pm.created.imports]
        assert all(e["attempts"][0]["result"] == "FakeTimeline" for e in res["formats"].values())
    assert len(names) == len(set(names)) == 8 and all("A" in n or "B" in n for n in names)


def test_retry_without_options(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media), none_with_options=True)
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=True)
    e = res["formats"]["fcp7xml"]
    assert [a["result"] for a in e["attempts"]] == [None, "FakeTimeline"]
    assert e["attempts"][0]["options"]["importSourceClips"] is True and e["attempts"][1]["options"] is None
    assert resolve.pm.created.imports[1] == (str(f.resolve()), None)
    assert e["checks"][0]["status"] == "pass" and "retry" in e["checks"][0]["detail"]


def test_import_failure_both_attempts_none(media, tmp_path):
    resolve = FakeResolve(None)
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=True)
    e = res["formats"]["fcp7xml"]
    assert e["passed"] is False and e["checks"][0]["status"] == "fail" and len(e["attempts"]) == 2
    assert e["diagnostics"]["resolve_return_type"] == "NoneType" and resolve.pm.deleted == []


def test_risky_formats_skipped_by_default(media, tmp_path):
    files = [tmp_path / n for n in ("p.fcpxml", "p.edl", "p.aaf", "p.xml")]
    for p in files: p.write_text("x")
    resolve = FakeResolve(make_actual_timeline(media))
    res = rv.run_import(resolve, files, make_expected(media), keep_project=True)
    assert [i[0] for i in resolve.pm.created.imports] == [str(files[3].resolve())]
    for k in ("fcpxml", "edl", "aaf"):
        e = res["formats"][k]
        assert e["skipped"] and e["checks"][0]["status"] == "info" and "--include-risky-formats" in e["checks"][0]["detail"]
        assert e["passed"] is False


def test_risky_formats_run_last_when_included(media, tmp_path):
    names = ["p.fcpxml", "p.edl", "p.aaf", "p.xml", "p.otio"]
    files = [tmp_path / n for n in names]
    for p in files: p.write_text("{}")
    resolve = FakeResolve(make_actual_timeline(media))
    rv.run_import(resolve, files, make_expected(media), keep_project=True, include_risky=True)
    order = [Path(i[0]).name for i in resolve.pm.created.imports]
    assert order == ["p.xml", "p.otio", "p.edl", "p.aaf", "p.fcpxml"]


# ---------------------------------------------------------------- OTIO variants
def relative_otio():
    d = synthetic_otio()
    d["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"] = "../../../tests/fixtures/generated/x.mp4"
    return d


def test_variant_formats_windows_and_posix():
    n = "C:/Users/me/My Media/x.mp4"
    assert rv.format_media_ref(n, "bare") == r"C:\Users\me\My Media\x.mp4"
    assert rv.format_media_ref(n, "slash") == n
    assert rv.format_media_ref(n, "file_url") == "file:///C:/Users/me/My%20Media/x.mp4"
    assert rv.format_media_ref("//srv/share/x.mp4", "bare") == r"\\srv\share\x.mp4"
    assert rv.format_media_ref("//srv/share/x.mp4", "file_url") == "file://srv/share/x.mp4"
    p = "/Users/me/My Media/x.mp4"
    assert rv.format_media_ref(p, "bare") == p and rv.format_media_ref(p, "slash") == p
    assert rv.format_media_ref(p, "file_url") == "file:///Users/me/My%20Media/x.mp4"


@pytest.mark.parametrize("base,expect", [
    ("C:/Users/me/spike/out/interchange", "C:/Users/me/tests/fixtures/generated/x.mp4"),
    ("/home/me/spike/out/interchange", "/home/me/tests/fixtures/generated/x.mp4"),
])
def test_rewrite_only_relative_targets(base, expect):
    doc = relative_otio()
    before = json.dumps(doc, sort_keys=True)
    n = rv.rewrite_otio_media(doc, base, "slash")
    # relative a.mp4 (../media) rewritten too; absolute file:// b.png and Missing/None refs untouched
    refs = doc["tracks"]["children"][0]["children"]
    assert n == 2  # x.mp4 (V1) and ../media/a.mp4 (A1)
    urls = [refs[0]["media_references"]["DEFAULT_MEDIA"]["target_url"], refs[3]["media_references"]["DEFAULT_MEDIA"]["target_url"]]
    assert urls[0] == expect and urls[1] == "file:///C:/Users/me/My%20Media/b.png"
    # everything except those target_urls is identical
    after = json.loads(json.dumps(doc))
    after["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"] = "../../../tests/fixtures/generated/x.mp4"
    after["tracks"]["children"][2]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"] = "../media/a.mp4"
    assert json.dumps(after, sort_keys=True) == before


def test_make_otio_variants_files(tmp_path):
    src = tmp_path / "spike" / "out" / "interchange"; src.mkdir(parents=True)
    f = write_otio(src, relative_otio())
    work = tmp_path / "work"
    out, notes = rv.make_otio_variants(f, work)
    assert set(out) == {"bare", "slash", "file_url"} and all(p.parent.parent == work for p in out.values())
    base = f.resolve().parent
    exp = rv.normalize_path("../../../tests/fixtures/generated/x.mp4", base, windows=False)
    got = {k: json.loads(p.read_text())["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"] for k, p in out.items()}
    assert got["slash"] == exp and got["file_url"] == "file://" + exp and got["bare"] == exp
    assert json.loads(f.read_text()) == relative_otio()  # original untouched
    # absolute-only OTIO -> no variants
    doc = synthetic_otio()
    for c in (doc["tracks"]["children"][0]["children"][0], doc["tracks"]["children"][2]["children"][0]):
        c["media_references"]["DEFAULT_MEDIA"]["target_url"] = "/abs/a.mp4"
    g = write_otio(tmp_path, doc, "abs.otio")
    out, notes = rv.make_otio_variants(g, tmp_path / "w2")
    assert out == {} and any("no relative" in n for n in notes)


def test_run_import_otio_variant_labels(media, tmp_path):
    f = write_otio(tmp_path, relative_otio())
    resolve = FakeResolve(make_actual_timeline(media))
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=True, work_dir=tmp_path / "w")
    assert list(res["formats"]) == ["otio[original]", "otio[bare]", "otio[slash]", "otio[file_url]"]
    assert Path(res["formats"]["otio[bare]"]["file"]).parent.parent == (tmp_path / "w").resolve()


def test_read_otio_expected_relative_path_resolved_absolute(tmp_path, monkeypatch):
    d = tmp_path / "spike" / "out" / "interchange"; d.mkdir(parents=True)
    write_otio(d, relative_otio())
    monkeypatch.chdir(tmp_path)
    e = rv.read_otio_expected(Path("spike/out/interchange/phase0.otio"))  # relative path argument
    want = rv.normalize_path(str(d.parent / "media" / "a.mp4"), windows=False)
    assert e["tracks"][2]["clips"][0]["source_path"] == want
    got = e["tracks"][0]["clips"][0]["source_path"]
    assert got == rv.normalize_path(str((d / "../../../tests/fixtures/generated/x.mp4").resolve()), windows=False)
    assert got.startswith("/") or got[1:3] == ":/"


# ---------------------------------------------------------------- cleanup
def test_cleanup_sequence_only_scratch_deleted(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media))
    resolve.pm.cur_folder = "Sub"
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False)
    pm = resolve.pm
    scratch = res["scratch_project"]
    close_i = pm.calls.index("CloseProject")
    assert pm.calls.index("LoadProject(UserProject)") > close_i
    assert pm.calls.index(f"DeleteProject({scratch})") > pm.calls.index("LoadProject(UserProject)")
    assert [c for c in pm.calls if c.startswith("DeleteProject")] == [f"DeleteProject({scratch})"]
    assert res["scratch_deleted"] and pm.cur == "UserProject"
    steps = [s["step"] for s in res["cleanup_steps"]]
    assert steps[:2] == ["close scratch project", "reload previous project"] and steps[-1] == "delete scratch project"
    assert all("result" in s for s in res["cleanup_steps"])


def test_cleanup_navigates_back_to_creation_folder(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media))
    pm = resolve.pm
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    orig_close = pm.CloseProject
    def close_and_move(p):
        pm.cur_folder = "root"; return orig_close(p)  # Resolve left us in another folder
    pm.CloseProject = close_and_move
    # make the previous project reachable regardless of folder
    pm.folders["root"]["UserProject"] = pm.folders["Sub"]["UserProject"]
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False)
    assert res["creation_folder"] == "Sub" and "OpenFolder(Sub)" in pm.calls
    assert res["scratch_deleted"] and pm.deleted == [res["scratch_project"]] and pm.cur_folder == "Sub"


def test_cleanup_without_previous_project_loads_other_then_deletes(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media), prev=None)
    pm = resolve.pm
    pm.folders["Sub"]["Other"] = FakeProject("Other", None)
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False)
    assert res["scratch_deleted"] and pm.deleted == [res["scratch_project"]]
    assert "LoadProject(Other)" in pm.calls and "LoadProject(Untitled Project)" not in pm.calls


def test_cleanup_failure_reports_manual_steps(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media), prev=None)  # nothing else to load -> cannot delete
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False)
    assert res["scratch_deleted"] is False and resolve.pm.deleted == []
    note = next(n for n in res["notes"] if "could not delete" in n)
    assert res["scratch_project"] in note and "Project Manager" in note and "Nothing else was deleted" in note


def test_keep_project_and_import_failure(media, tmp_path):
    resolve = FakeResolve(None)  # ImportTimelineFromFile -> None
    f = tmp_path / "t.xml"; f.write_text("<x/>")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=True)
    assert resolve.pm.deleted == [] and res["formats"]["fcp7xml"]["passed"] is False
    assert res["formats"]["fcp7xml"]["checks"][0]["status"] == "fail"


def test_main_with_fake_resolve_exit_codes(media, tmp_path, monkeypatch):
    f = tmp_path / "t.otio"; f.write_text("{}")
    exp = tmp_path / "e.json"; exp.write_text(json.dumps(make_expected(media)))
    for tl, code in ((make_actual_timeline(media), 0), (make_actual_timeline(media, fps="24"), 1)):
        monkeypatch.setattr(rv, "connect_resolve", lambda *a, _r=FakeResolve(tl), **k: (_r, {"diagnosis": []}))
        out = tmp_path / f"r{code}.json"
        assert rv.main(["--timeline", str(f), "--expected", str(exp), "--output", str(out)]) == code
        rep = json.loads(out.read_text())
        assert rep["resolve"]["version"] == "19.1.0" and rep["status"] == ("pass" if code == 0 else "fail")
