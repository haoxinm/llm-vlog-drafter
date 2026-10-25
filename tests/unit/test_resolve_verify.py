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
    def __init__(self, name, timeline):
        self.name, self.timeline, self.settings = name, timeline, {}

    def GetName(self): return self.name
    def SetSetting(self, k, v): self.settings[k] = v; return True
    def GetMediaPool(self): return self
    def ImportTimelineFromFile(self, path, opts): self.last = (path, opts); return self.timeline
    def SetCurrentTimeline(self, tl): return True


class FakePM:
    def __init__(self, timeline, prev="UserProject"):
        self.timeline, self.projects, self.cur = timeline, {prev: FakeProject(prev, None)}, prev
        self.deleted: list[str] = []

    def GetCurrentProject(self): return self.projects[self.cur]
    def CreateProject(self, n):
        self.projects[n] = FakeProject(n, self.timeline); self.cur = n; return self.projects[n]
    def CloseProject(self, p): return True
    def LoadProject(self, n): self.cur = n; return self.projects[n]
    def DeleteProject(self, n):
        if n == "UserProject": raise AssertionError("must never delete user project")
        self.deleted.append(n); self.projects.pop(n); return True


class FakeResolve:
    def __init__(self, timeline): self.pm = FakePM(timeline)
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
def test_run_import_end_to_end_and_cleanup(media, tmp_path):
    resolve = FakeResolve(make_actual_timeline(media))
    f = tmp_path / "t.otio"; f.write_text("{}")
    res = rv.run_import(resolve, [f], make_expected(media), keep_project=False)
    assert res["formats"]["otio"]["passed"]
    pm = resolve.pm
    assert len(pm.deleted) == 1 and pm.deleted[0] == res["scratch_project"] and pm.cur == "UserProject"
    assert "UserProject" in pm.projects
    proj_settings = None
    assert res["scratch_deleted"] is True
    table = rv.markdown_table(res["formats"])
    assert "| clip_order |" in table and "PASS" in table


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
