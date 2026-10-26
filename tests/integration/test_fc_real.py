"""Integration tests against the real filmcraft-cli. Needs $FILMCRAFT_CLI (skipped cleanly otherwise) and ffmpeg."""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "filmcraft"))
from conftest import hash_tree  # noqa: E402
from trip_vlog.filmcraft import interchange as ix  # noqa: E402
from trip_vlog.filmcraft.adapter import FilmCraftAdapter  # noqa: E402
from trip_vlog.filmcraft.execution import load_journal  # noqa: E402
from trip_vlog.filmcraft.inspect import verify_against_spec  # noqa: E402
from trip_vlog.filmcraft.timeline_spec import MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn  # noqa: E402

CLI = os.environ.get("FILMCRAFT_CLI")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")]


def make_spec(media: Path, fps=Fraction(30), marker_frame=60) -> TimelineSpec:
    P = lambda n: str(media / n)
    return TimelineSpec(
        "Main Draft", 1920, 1080, fps, ("V1", "V2", "A1", "A2"),
        placements=(
            Placement("land", P("landscape_5s.mp4"), "video", "V1", 0, 105, 0.5, 4.0, True),
            Placement("still", P("still_1080p.png"), "image", "V1", 105, 90, 0.0, None, False, None, TransitionIn("dissolve", 15)),
            Placement("port", P("portrait_4s.mp4"), "video", "V2", 60, 60, 1.0, 3.0, False),
            Placement("wav", P("music_6s.wav"), "audio", "A2", 0, 180, 0.0, 6.0, False, -6.0),
        ),
        markers=(MarkerSpec(marker_frame, "Scene A", "intro"),),
        titles=(TitleSpec("Hello Title", 0, 60, "V2"),))


def new_adapter(ws: Path, media: Path) -> FilmCraftAdapter:
    return FilmCraftAdapter(ws, media, executable=CLI, request_timeout=120)


@pytest.fixture(scope="module")
def built(tmp_path_factory, ffmpeg_tools, generated_media):
    root = tmp_path_factory.mktemp("fcreal")
    media = root / "media"
    shutil.copytree(generated_media, media)
    before = hash_tree(media)
    ws = root / "workspace"
    adapter = new_adapter(ws, media)
    spec = make_spec(media)
    res = adapter.build_project(spec)
    yield {"adapter": adapter, "spec": spec, "res": res, "media": media, "ws": ws, "before": before}
    adapter.close()


def test_build_saved_reopened_and_verified(built):
    res, ws = built["res"], built["ws"]
    assert not res.noop and res.version == 1 and res.verification.ok, res.verification and res.verification.summary()
    assert res.project_path == ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj" and res.project_path.stat().st_size > 1000
    run = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"][0]
    assert run["status"] == "complete" and run["verified_after_reopen"] and run["filmcraft_version"]
    assert (ws / "filmcraft" / "capability_snapshot.json").is_file()
    snap = json.loads((ws / "filmcraft" / "capability_snapshot.json").read_text(encoding="utf-8"))
    assert snap["missing_required"] == [] and snap["filmcraft"]["version"] == run["filmcraft_version"]


def test_independent_process_reads_back_exactly_the_spec(built):
    other = new_adapter(built["ws"], built["media"])
    try:
        other.open_project(built["res"].project_path)
        seq = other.inspect_sequence()
        assert seq.clip_count == 6 and [len(t.clips) for t in seq.tracks if t.name in ("V1", "V2", "A1", "A2")] == [2, 2, 1, 1]
        assert [c.name for c in seq.track("V1").clips] == ["landscape_5s.mp4", "still_1080p.png"]
        rep = verify_against_spec(seq, built["spec"], project=other.inspect_project())
        assert rep.ok, rep.summary()
        assert {m.status for m in other.inspect_project().media} == {"online"}
        a2 = seq.track("A2").clips[0]
        assert a2.gain_db == -6.0 and len(seq.transitions) == 1 and len(seq.markers) == 1
    finally:
        other.close()


def test_rerun_is_a_noop_and_never_duplicates_clips(built):
    adapter = new_adapter(built["ws"], built["media"])
    try:
        res = adapter.build_project(built["spec"])
        assert res.noop and res.version == 1
        assert [p.name for p in (built["ws"] / "filmcraft").glob("*.fcproj")] == ["Trip Vlog Draft_v001.fcproj"]
        adapter.open_project(res.project_path)
        assert adapter.inspect_sequence().clip_count == 6
    finally:
        adapter.close()


def test_changed_spec_gets_a_new_version_and_old_one_is_untouched(built):
    old = built["res"].project_path.read_bytes()
    adapter = new_adapter(built["ws"], built["media"])
    try:
        res = adapter.build_project(make_spec(built["media"], marker_frame=90))
        assert not res.noop and res.version == 2 and res.verification.ok
        assert built["res"].project_path.read_bytes() == old
    finally:
        adapter.close()


def test_preview_renders_and_ffprobe_verifies(built):
    adapter = built["adapter"]
    rep = adapter.render_preview(project_path=built["res"].project_path)
    assert rep.ok, [c.to_dict() for c in rep.failures]
    assert Path(rep.path) == built["ws"] / "preview" / "draft.mp4"
    assert (rep.width, rep.height, rep.fps, rep.video_codec) == (1920, 1080, "30/1", "h264")
    assert rep.nb_frames == 195 and rep.audio_streams >= 1 and abs(rep.duration_seconds - 6.5) < 0.1
    assert (built["ws"] / "filmcraft" / "FilmCraft Previews").is_dir()  # FilmCraft's scratch dir stays in the workspace


def test_interchange_otio_and_xml_have_absolute_media_paths(built):
    adapter = built["adapter"]
    rep = adapter.export_interchange(spec=built["spec"], project_path=built["res"].project_path)
    assert rep.ok, [(f.format, [c.to_dict() for c in f.checks if not c.ok], f.error) for f in rep.formats]
    otio = built["ws"] / "resolve_handoff" / "timeline.otio"
    doc = json.loads(otio.read_text(encoding="utf-8"))
    urls = sorted({d["target_url"] for d in ix._walk_dicts(doc) if "target_url" in d})
    assert len(urls) == 4
    media_files = {p.resolve() for p in built["media"].iterdir()}
    for u in urls:
        assert u.startswith("file:///") and ".." not in u
        assert Path(ix.url_to_path(u)).resolve() in media_files
    xml = (built["ws"] / "resolve_handoff" / "timeline.xml").read_text(encoding="utf-8")
    assert "file://" in xml and "../" not in xml
    assert rep.titles_not_carried[0]["text"] == "Hello Title"
    assert any("Titles are not carried" in l for f in rep.formats for l in f.loses)
    assert any("no OTIO equivalent" in e for f in rep.formats for e in f.engine_report)


def test_otio_library_reload_when_installed(built):
    otio_lib = pytest.importorskip("opentimelineio")
    tl = otio_lib.adapters.read_from_file(str(built["ws"] / "resolve_handoff" / "timeline.otio"))
    assert tl.duration().to_frames() == 195


def test_originals_untouched_and_log_sanitized(built):
    assert hash_tree(built["media"]) == built["before"]
    text = (built["ws"] / "filmcraft" / "command_log.jsonl").read_text(encoding="utf-8")
    assert str(built["media"]) not in text and str(built["ws"]) not in text and "$MEDIA_ROOT" in text
    assert len(text.splitlines()) > 50


def test_paths_outside_roots_refused_without_touching_filmcraft(built, tmp_path):
    adapter = new_adapter(tmp_path / "ws2", built["media"])
    stray = tmp_path / "stray.mp4"
    shutil.copy(built["media"] / "landscape_5s.mp4", stray)
    spec = make_spec(built["media"])
    bad = replace(spec, placements=(replace(spec.placements[0], source_path=str(stray)),) + spec.placements[1:])
    try:
        from trip_vlog.filmcraft.errors import PathPolicyError

        with pytest.raises(PathPolicyError):
            adapter.build_project(bad)
        assert not list((tmp_path / "ws2").rglob("*.fcproj"))
        adapter.build_project(spec)
        with pytest.raises(PathPolicyError):
            adapter.render_preview(tmp_path / "evil.mp4")
        assert not (tmp_path / "evil.mp4").exists()
    finally:
        adapter.close()


def test_ntsc_frame_rate_is_exact(built, tmp_path):
    adapter = new_adapter(tmp_path / "ws_ntsc", built["media"])
    try:
        spec = make_spec(built["media"], fps=Fraction(30000, 1001))
        res = adapter.build_project(spec)
        assert res.verification.ok and adapter.inspect_sequence().fps == Fraction(30000, 1001)
    finally:
        adapter.close()


def test_source_past_end_rejected_before_any_clip_is_placed(built, tmp_path):
    adapter = new_adapter(tmp_path / "ws_bad", built["media"])
    try:
        from trip_vlog.filmcraft.errors import SpecValidationError

        spec = make_spec(built["media"])
        bad = replace(spec, placements=(replace(spec.placements[0], source_in_seconds=4.0, source_out_seconds=7.5, duration_frames=105),) + spec.placements[1:])
        with pytest.raises(SpecValidationError, match="past the end"):
            adapter.build_project(bad)
        recs = [json.loads(l) for l in (tmp_path / "ws_bad" / "filmcraft" / "command_log.jsonl").read_text().splitlines()]
        assert not [r for r in recs if r.get("command") == "timeline.place"]
    finally:
        adapter.close()


def test_exported_fcp7_xml_round_trips_the_spec_exactly(built):
    """Phase 5a: parse the XML FilmCraft really exports (dissolve, still, V2 overlay and title, gain, music, marker) and compare at frame precision."""
    from trip_vlog.handoff.fcp7_xml import parse_fcp7_xml
    from trip_vlog.handoff.xml_roundtrip import compare_timeline_xml, expected_from_spec

    rep = built["adapter"].export_interchange(["fcp7_xml"], built["ws"] / "xml_roundtrip", spec=built["spec"], project_path=built["res"].project_path)
    xml = parse_fcp7_xml(next(Path(f) for f in rep.files if f.endswith(".xml")))
    rt = compare_timeline_xml(xml, expected_from_spec(built["spec"]))
    assert rt.ok, [str(m) for m in rt.mismatches]
    assert rt.matched["dissolves"] == 1 and rt.matched["titles"] == 1 and rt.matched["markers"] == 1 and rt.matched["music_track"] == 1
    assert rt.matched["stills"] == 1 and rt.matched["overlays_v2"] == 1 and rt.matched["audio_gain"] == 1
