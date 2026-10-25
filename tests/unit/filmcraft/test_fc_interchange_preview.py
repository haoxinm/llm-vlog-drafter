from __future__ import annotations

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from trip_vlog.filmcraft import adapter as adapter_mod
from trip_vlog.filmcraft import interchange as ix
from trip_vlog.filmcraft.errors import ParameterError, PathPolicyError
from trip_vlog.filmcraft.mappings import canon_path
from trip_vlog.filmcraft.preview import evaluate_preview


def test_url_and_canon_helpers_cover_windows_and_posix_forms():
    assert ix.url_to_path("file://localhost/Users/me/a%20b.mov") == "/Users/me/a b.mov"
    assert ix.url_to_path("file:///C:/Users/me/a%20b.mov") == "C:/Users/me/a b.mov"
    assert ix.url_to_path("file://server/share/x.mov") == "//server/share/x.mov"
    assert ix.is_absolute_ref("C:\\x\\y.mp4") and ix.is_absolute_ref("/x/y") and not ix.is_absolute_ref("../x.mp4")
    assert canon_path("file:///C:/Users/me/x.mp4") == canon_path("file://localhost/C:/Users/me/x.mp4")
    assert canon_path("\\\\?\\C:\\a\\b.mp4").lower().endswith("c:/a/b.mp4")


def test_relative_otio_urls_become_absolute_file_urls(tmp_path):
    media = tmp_path / "media" / "day 1"
    media.mkdir(parents=True)
    clip = media / "a b.mp4"
    clip.write_bytes(b"x")
    out = tmp_path / "ws" / "resolve_handoff"
    out.mkdir(parents=True)
    rel = "../../media/day%201/a%20b.mp4"
    doc = {"OTIO_SCHEMA": "Timeline.1", "tracks": {"children": [{"children": [
        {"OTIO_SCHEMA": "Clip.2", "media_references": {"D": {"OTIO_SCHEMA": "ExternalReference.1", "target_url": rel}}},
        {"OTIO_SCHEMA": "Clip.2", "media_references": {"D": {"OTIO_SCHEMA": "MissingReference.1"}}},
        {"OTIO_SCHEMA": "Clip.2", "media_references": {"D": {"OTIO_SCHEMA": "ExternalReference.1", "target_url": "file:///nowhere/x.mp4"}}}]}]}}
    p = out / "t.otio"
    p.write_text(json.dumps(doc), encoding="utf-8")
    actions, warns = ix.rewrite_otio_urls(p, {canon_path(clip): str(clip)})
    new = json.loads(p.read_text(encoding="utf-8"))
    urls = [r["target_url"] for r in (c["media_references"]["D"] for c in new["tracks"]["children"][0]["children"]) if "target_url" in r]
    assert urls[0] == clip.as_uri() and urls[0].startswith("file:///") and "%20" in urls[0]
    assert urls[1] == "file:///nowhere/x.mp4" and actions and len(warns) == 1 and "could not be resolved" in warns[0]  # left alone, but reported
    # idempotent
    assert ix.rewrite_otio_urls(p, {canon_path(clip): str(clip)})[0] == []


def test_unresolvable_reference_warns_and_filename_fallback(tmp_path):
    clip = tmp_path / "m" / "a.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    known = {canon_path(clip): str(clip)}
    t, w = ix.resolve_reference("../wrong/dir/a.mp4", tmp_path / "out", known)
    assert t == clip and "matched by file name" in w
    t, w = ix.resolve_reference("../x/zzz.mp4", tmp_path / "out", known)
    assert t is None and "could not be resolved" in w
    t, w = ix.resolve_reference("https://example.com/a.mp4", tmp_path, known)
    assert t is None and "non-file" in w


def test_edl_and_xml_rewrites(tmp_path):
    clip = tmp_path / "media" / "a.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    known = {canon_path(clip): str(clip)}
    out = tmp_path / "out"
    out.mkdir()
    edl = out / "t.edl"
    edl.write_text("TITLE: x\n001  AX V C 00:00:00:00 00:00:01:00 00:00:00:00 00:00:01:00\n* SOURCE FILE: ../media/a.mp4\n", encoding="utf-8")
    assert ix.rewrite_edl_sources(edl, known)[0]
    assert f"* SOURCE FILE: {clip}" in edl.read_text(encoding="utf-8")
    xml = out / "t.xml"
    xml.write_text("<xmeml><file><pathurl>../media/a.mp4</pathurl></file><file><pathurl>file://localhost/abs/b.mp4</pathurl></file></xmeml>", encoding="utf-8")
    assert ix.rewrite_xml_pathurls(xml, known)[0]
    text = xml.read_text(encoding="utf-8")
    assert clip.as_uri() in text and "file://localhost/abs/b.mp4" in text


def test_export_through_fake_rewrites_validates_and_reports_losses(fc, monkeypatch):
    monkeypatch.setitem(sys.modules, "opentimelineio", None)  # the fake's OTIO is structural only, not library-valid
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    rep = adapter.export_interchange(spec=spec)
    assert [f.format for f in rep.formats] == ["fcp7_xml", "otio"] and rep.ok
    otio = next(f for f in rep.formats if f.format == "otio")
    assert otio.role == "primary" and otio.postprocess and otio.engine_report and any("Titles are not carried" in l for l in otio.loses)
    assert Path(otio.path) == ws / "resolve_handoff" / "timeline.otio"
    doc = json.loads(Path(otio.path).read_text(encoding="utf-8"))
    urls = [d["target_url"] for d in ix._walk_dicts(doc) if "target_url" in d]
    assert urls and all(u.startswith("file://") and Path(ix.url_to_path(u)).is_file() for u in urls)
    assert rep.titles_not_carried == [{"text": "Hello Title", "start_frame": 0, "duration_frames": 60, "track": "V2"}]
    assert rep.primary().format == "otio" and len(rep.files) == 2 and rep.to_dict()["ok"]
    assert any("verified" in l for l in rep.limitations)


def test_export_unknown_format_and_outside_workspace(fc, tmp_path):
    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    with pytest.raises(ValueError, match="unknown interchange format"):
        adapter.export_interchange(["pdf"])
    with pytest.raises(PathPolicyError):
        adapter.export_interchange(["otio"], tmp_path / "elsewhere")
    assert ix.resolve_format("xml").key == "fcp7_xml" and ix.resolve_format("fcp7_xml").fc_format == "xml"


def test_validation_detects_a_broken_export(fc):
    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    rep = adapter.export_interchange(["otio"])
    p = Path(rep.formats[0].path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    for d in ix._walk_dicts(doc):
        if "target_url" in d:
            d["target_url"] = "../relative/again.mp4"
    p.write_text(json.dumps(doc), encoding="utf-8")
    snap = adapter.inspect_sequence()
    checks = ix.validate_otio(p, snap, {})
    assert any(not c.ok and "absolute" in c.name for c in checks)


# ---- preview -------------------------------------------------------------------------------------------------------

def probe(w=1920, h=1080, fps="30/1", frames=195, dur="6.500000", audio=True):
    s = [{"codec_type": "video", "codec_name": "h264", "width": w, "height": h, "r_frame_rate": fps, "nb_frames": str(frames)}]
    if audio:
        s.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": s, "format": {"duration": dur}}


def ev(p, **kw):
    base = dict(path="x.mp4", width=1920, height=1080, fps=Fraction(30), frames=195, expect_audio=True)
    return evaluate_preview(p, **{**base, **kw})


def test_evaluate_preview_pass_and_each_failure():
    assert ev(probe()).ok
    assert ev(probe(fps="30000/1001", dur="6.506"), fps=Fraction(30000, 1001)).ok
    for p, name in [(probe(w=1280, h=720), "dimensions"), (probe(fps="25/1"), "frame rate"), (probe(frames=150), "frame count"),
                    (probe(dur="9.0"), "duration"), (probe(audio=False), "audio stream present")]:
        rep = ev(p)
        assert not rep.ok and name in [c.name for c in rep.failures]
    assert ev(probe(audio=False), expect_audio=False).ok
    assert ev(probe(w=960, h=540), scale=0.5).ok
    assert not ev({"streams": [], "format": {}}).ok


def test_render_preview_command_and_report(fc, monkeypatch):
    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    monkeypatch.setattr(adapter_mod, "run_ffprobe", lambda path, ffprobe=None: probe(frames=195, dur="6.5"))
    rep = adapter.render_preview()
    assert rep.ok and rep.path == str(ws / "preview" / "draft.mp4")
    (req,) = fcsupport.log_requests(ws, "file.exportMedia")
    args = req["params"]["arguments"]["params"]
    assert args["wait"] is True and args["format"] == "h264" and args["path"].startswith("$WORKSPACE")
    rep2 = adapter.render_preview(scale=0.5)
    assert fcsupport.log_requests(ws, "file.exportMedia")[-1]["params"]["arguments"]["params"]["scale"] == 0.5
    assert not rep2.ok  # canned probe is 1920x1080, a half-scale render must be 960x540


def test_render_preview_rejects_bad_targets(fc, tmp_path):
    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    with pytest.raises(PathPolicyError):
        adapter.render_preview(tmp_path / "outside.mp4")
    with pytest.raises(PathPolicyError):
        adapter.render_preview(media / "overwrite_original.mp4")
    with pytest.raises(ParameterError, match=".mp4"):
        adapter.render_preview(ws / "preview" / "x.mov")
    with pytest.raises(ParameterError, match="scale"):
        adapter.render_preview(scale=2)
