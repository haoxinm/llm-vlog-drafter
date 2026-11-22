"""Phase 7b against the real filmcraft-cli: scale-to-fit framing, Volume-keyframe ducking (read-back, native XML, rendered music bed) and the
read-back FCP7 XML fallback. Needs $FILMCRAFT_CLI and ffmpeg."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "filmcraft"))
from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.timeline_spec import GainKey, Placement, TimelineSpec
from trip_vlog.handoff import readback as RB
from trip_vlog.handoff import xml_roundtrip as X
from trip_vlog.handoff.fcp7_xml import parse_fcp7_xml
from trip_vlog.qc.checks import PASS
from trip_vlog.qc.ducking import DuckPlan, DuckSettings, DuckWindow, check_music_ducking
from trip_vlog.qc.framing import check_framing

CLI = os.environ.get("FILMCRAFT_CLI")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")]

KEYS = ((30, 0.0), (36, -12.0), (120, -12.0), (126, 0.0))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def world(tmp_path_factory, ffmpeg_tools, generated_media):
    root = tmp_path_factory.mktemp("hb")
    media = root / "media"
    shutil.copytree(generated_media, media)
    P = lambda n: str(media / n)
    spec = TimelineSpec("Main Draft", 1920, 1080, Fraction(30), ("V1", "V2", "A1", "A2"), placements=(
        Placement("land", P("landscape_5s.mp4"), "video", "V1", 0, 90, 0.5, 3.5, True),
        Placement("port", P("portrait_4s.mp4"), "video", "V1", 90, 60, 1.0, 3.0, False, fit_source_size=(1080, 1920)),
        Placement("still", P("still_1080p.png"), "image", "V1", 150, 30),
        Placement("music", P("music_6s.wav"), "audio", "A2", 0, 180, 0.0, 6.0, False, -6.0, gain_keys=tuple(GainKey(f, d) for f, d in KEYS))))
    ad = FilmCraftAdapter(root / "ws", media, executable=CLI, request_timeout=120)
    res = ad.build_project(spec)
    yield {"ad": ad, "spec": spec, "res": res, "media": media, "ws": root / "ws", "ffmpeg": ffmpeg_tools[0]}
    ad.close()


def test_fit_and_keyframes_verified_after_reopen(world):
    assert world["res"].verification.ok, world["res"].verification.summary()
    assert world["res"].verification.checked["framing"] == 1 and world["res"].verification.checked["ducking"] == 1
    ad = world["ad"]
    ad.open_project(world["res"].project_path)
    seq = ad.inspect_sequence()
    port = next(c for c in seq.track("V1").clips if c.name == "portrait_4s.mp4")
    assert port.scale == pytest.approx(56.25) and all(c.scale == 100.0 for c in seq.track("V1").clips if c is not port)
    assert seq.track("A2").clips[0].volume_keyframes == 4


def test_native_xml_carries_scale_and_keyframes_exactly(world):
    ad, spec = world["ad"], world["spec"]
    rep = ad.export_interchange(["fcp7_xml"], world["ws"] / "handoff", spec=spec, project_path=world["res"].project_path)
    x = Path(rep.formats[0].path)
    tl = parse_fcp7_xml(x)
    rt = X.compare_timeline_xml(tl, X.expected_from_spec(spec))
    assert rt.ok, [str(m) for m in rt.mismatches]
    assert rt.matched[X.F_FRAMING] == 1 and rt.matched[X.F_DUCKING] == 1


def test_rendered_preview_has_black_bars_and_the_music_bed_is_ducked(world):
    ad, spec, ws = world["ad"], world["spec"], world["ws"]
    proj = world["res"].project_path
    rep = ad.render_preview(project_path=proj)
    assert rep.ok
    r = check_framing(spec, ad.inspect_sequence(), ws / "preview" / "draft.mp4", world["ffmpeg"])
    assert r.status == PASS and r.data["preview_frames"] == 1, r.detail
    before = sha(proj)
    bed = ad.render_music_bed(ws / "preview" / "music_bed.wav", ["A2"], project_path=proj)
    assert sha(proj) == before, "measuring must not write the project"
    plan = SimpleNamespace(sequence=SimpleNamespace(fps="30"), scenes=[SimpleNamespace(clips=[SimpleNamespace(
        clip_instance_id="music", timeline_start_frame=0, duration_frames=180, source_in_seconds=0.0)])])
    duck = DuckPlan([DuckWindow(36, 120, "speech", ("land",))], {"music": tuple(GainKey(f, d) for f, d in KEYS)},
                    settings=DuckSettings(True, -12.0, 0.2, 1.0, "speech"))
    res = check_music_ducking(plan, duck, bed, {"music": spec.placements[3].source_path}, world["ffmpeg"], music_gain_db={"music": -6.0})
    assert res.status == PASS, (res.detail, res.data)
    # and the check is not a rubber stamp: the same bed against an envelope that ducks somewhere else fails it
    elsewhere = DuckPlan([DuckWindow(10, 28, "speech", ("land",))], {"music": duck.keys["music"]}, settings=duck.settings)
    plan.scenes[0].clips[0].duration_frames = 180
    wrong = check_music_ducking(plan, DuckPlan([DuckWindow(130, 175, "speech", ("land",))], elsewhere.keys, settings=duck.settings), bed,
                                {"music": spec.placements[3].source_path}, world["ffmpeg"], music_gain_db={"music": -6.0})
    assert wrong.status != PASS


def test_readback_fallback_on_the_real_engine(world):
    ad, spec = world["ad"], world["spec"]
    out = world["ws"] / "handoff_rb"
    out.mkdir()
    rec = RB.run_fallback(ad, spec, ["fcp7_xml"], out, world["res"].project_path, mode="auto", listed_error="simulated: FilmCraft export missing")
    assert rec["status"] == "generated", rec
    assert rec["crosscheck"]["ok"], rec["crosscheck"]
    tl = parse_fcp7_xml(out / "timeline.xml")
    assert X.compare_timeline_xml(tl, X.expected_from_spec(spec), keyframes_carried=False).ok
    assert any("volume keyframe" in n for n in rec["not_carried"])
    # the generated file and FilmCraft's own export describe the same clips
    native = parse_fcp7_xml(Path(world["ad"].export_interchange(["fcp7_xml"], world["ws"] / "handoff_native", spec=spec,
                                                                project_path=world["res"].project_path).formats[0].path))
    key = lambda t: sorted((c.kind, c.track, c.start, c.end, c.source_in, c.source_out, c.path) for c in t.clips)
    assert key(tl) == key(native)


def test_pipeline_with_fit_mixed_aspect_scales_the_portrait_clip_and_reports_it(tmp_path, generated_media):
    import json

    from trip_vlog.orchestrator import run_draft

    media = tmp_path / "media"
    shutil.copytree(generated_media, media)
    ws = tmp_path / "ws"
    res = run_draft(media, ws, None, {"project.target_duration_seconds": 12, "workflow.approval": "auto_approve", "filmcraft.executable": CLI,
                                      "filmcraft.fit_mixed_aspect": True, "filmcraft.duck_music": True}, echo=lambda *_: None)
    assert res.exit_code == 0 and res.stage_status["qc"] == "succeeded"
    qc = {c["name"]: c for c in json.loads((ws / "preview" / "qc_report.json").read_text(encoding="utf-8"))["checks"]}
    assert qc["framing"]["status"] == "pass" and qc["framing"]["data"]["preview_frames"] >= 1
    assert qc["music_ducking"]["status"] == "skipped"          # the baseline plan has no music
    compat = json.loads((ws / "resolve_handoff" / "compatibility_report.json").read_text(encoding="utf-8"))
    rows = {r["feature"]: r for r in compat["features"]}
    assert rows["framing_fit"]["plan_count"] == 1 and rows["framing_fit"]["xml"]["matched"] == 1 and compat["xml"]["origin"] == "native"
    assert not (ws / "resolve_handoff" / "readback_interchange.json").exists()      # a good native export is never replaced
    # the settings are part of the stage cache keys: turning framing off builds a new, unscaled project version; the same settings change nothing
    run_draft(media, ws, None, {"project.target_duration_seconds": 12, "workflow.approval": "auto_approve", "filmcraft.executable": CLI,
                                "filmcraft.fit_mixed_aspect": True, "filmcraft.duck_music": True}, echo=lambda *_: None)
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj"]
    run_draft(media, ws, None, {"project.target_duration_seconds": 12, "workflow.approval": "auto_approve", "filmcraft.executable": CLI,
                                "filmcraft.fit_mixed_aspect": False, "filmcraft.duck_music": False}, echo=lambda *_: None)
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj", "Trip Vlog Draft_v002.fcproj"]
