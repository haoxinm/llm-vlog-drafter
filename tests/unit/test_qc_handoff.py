import csv
import json
from fractions import Fraction
from pathlib import Path

from planning_helpers import make_catalog, mid, sel_clip, sel_plan

from trip_vlog.filmcraft.inspect import ClipSnapshot, SequenceSnapshot, TrackSnapshot
from trip_vlog.filmcraft.interchange import FormatReport, HandoffReport
from trip_vlog.handoff.interchange import HandoffExport
from trip_vlog.handoff.media_manifest import COLUMNS, manifest_rows, missing_used_media, write_media_manifest
from trip_vlog.handoff.report import HandoffReportInputs, read_resolve_verification, render_handoff_report
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan
from trip_vlog.qc import checks as C
from trip_vlog.qc.preview import sample_times

CAT = make_catalog()


def clip(cid, start, end, track="V1"):
    return ClipSnapshot(cid, 1, f"c{cid}", track, "video", start, end, end - start, 0, 0, 0, 0, 0.0, 1.0, None, True)


def seq(*spans, frames=None):
    clips = tuple(clip(i, a, b) for i, (a, b) in enumerate(spans, 1))
    return SequenceSnapshot(1, "S", 1920, 1080, Fraction(30), frames or max(b for _, b in spans),
                            (TrackSnapshot("V1", "Video 1", "video", 1, clips, ()),), ())


def test_v1_gap_detection():
    assert C.check_v1_gaps(seq((0, 90), (90, 180))).status == C.PASS
    r = C.check_v1_gaps(seq((0, 90), (100, 180)))
    assert r.status == C.FAIL and r.data["gaps"] == [{"at_frame": 90, "frames": 10}]
    assert C.check_v1_gaps(seq((5, 90))).status == C.FAIL  # leading gap
    assert C.check_v1_gaps(seq((0, 90), (80, 180))).status == C.WARN  # overlap


def test_total_duration():
    assert C.check_total_duration(seq((0, 300)), 10.0, 5, 2).status == C.PASS
    assert C.check_total_duration(seq((0, 300)), 20.0, 5, 2).status == C.FAIL


def test_stills_duplicates_orientation():
    plan = resolve_selection_plan(sel_plan([sel_clip("c1", mid(CAT, "day1/c.jpg"), 1.0), sel_clip("c2", mid(CAT, "day2/p.mp4", 0), 3.0),
                                            sel_clip("c3", mid(CAT, "day2/p.mp4", 0), 3.0)]), CAT, PlanSettings(target_duration_seconds=7))
    assert C.check_stills(plan, 1.5).status == C.FAIL and C.check_stills(plan, 0.5).status == C.PASS
    assert C.check_duplicates(plan, CAT).status == C.WARN
    o = C.check_orientation(plan, CAT)
    assert o.status == C.WARN and len(o.data["clips"]) == 2


def test_blackdetect_parse_and_status():
    out = "[blackdetect @ 0x1] black_start:2.5 black_end:3.5 black_duration:1\n"
    iv = C.parse_blackdetect(out)
    assert iv == [{"start": 2.5, "end": 3.5, "duration": 1.0}]
    assert C.check_black_frames(iv).status == C.WARN and C.check_black_frames([]).status == C.PASS
    assert C.check_black_frames(None, "no ffmpeg").status == C.WARN


def test_project_reopens_and_overall():
    assert C.check_project_reopens(True, []).status == C.PASS
    assert C.check_project_reopens(True, ["x"]).status == C.FAIL
    assert C.check_project_reopens(False, [], "boom").status == C.FAIL
    assert C.overall_status([C.CheckResult("a", C.PASS), C.CheckResult("b", C.WARN)]) == C.WARN
    assert C.overall_status([C.CheckResult("a", C.FAIL), C.CheckResult("b", C.WARN)]) == C.FAIL


def test_sample_times_around_cuts_and_capped():
    pts = sample_times([5.0, 9.0], 12.0, 48)
    assert [round(t, 2) for t, _ in pts] == [0.0, 4.85, 5.15, 8.85, 9.15, 11.95]
    assert len(sample_times([float(i) for i in range(1, 100)], 100.0, 20)) == 20


def test_media_manifest_csv(tmp_path):
    rows_in = [{"asset_id": "asset_1", "relative_path": "a/b.mp4", "media_type": "video", "content_version": "fast:x", "sha256": None,
                "duration_seconds": 5.0, "width": 1920, "height": 1080, "frame_rate_num": 30000, "frame_rate_den": 1001, "probe_status": "ok"},
               {"asset_id": "asset_2", "relative_path": "c.png", "media_type": "image", "content_version": "fast:y", "sha256": "ab" * 32,
                "duration_seconds": None, "width": 10, "height": 10, "frame_rate_num": None, "frame_rate_den": None, "probe_status": "ok"}]
    rows = manifest_rows(rows_in, tmp_path, ["asset_1"])
    out = tmp_path / "m.csv"
    write_media_manifest(out, rows)
    assert b"\r" not in out.read_bytes()
    got = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert list(got[0]) == list(COLUMNS)
    assert got[0]["fps"] == "30000/1001" and got[0]["used_in_timeline"] == "yes" and got[0]["exists_on_disk"] == "no"
    assert got[0]["absolute_path"] == str(tmp_path / "a" / "b.mp4") and got[1]["sha256"] == "ab" * 32
    assert missing_used_media(rows) == [str(tmp_path / "a" / "b.mp4")]


def test_resolve_verification_states(tmp_path):
    p = tmp_path / "resolve_verification.json"
    assert read_resolve_verification(p)[0] == "absent"
    p.write_text(json.dumps({"status": "manual_required"}), encoding="utf-8")
    assert read_resolve_verification(p)[0] == "not_passing"
    p.write_text(json.dumps({"status": "pass", "resolve": {"product": "DaVinci Resolve", "version": "21.1"}}), encoding="utf-8")
    assert read_resolve_verification(p)[0] == "passing"
    p.write_text("{nope", encoding="utf-8")
    assert read_resolve_verification(p)[0] == "unreadable"


def _inputs(tmp_path, export, verification=("absent", ""), missing=()):
    return HandoffReportInputs("T", "0.13.0", "0.1.0", "Linux x86_64", tmp_path, export, None, None, "ab" * 32, "warn", [
        {"used_in_timeline": "yes"}], list(missing), verification=verification)


def test_report_failure_fallback_and_unverified(tmp_path):
    text = render_handoff_report(_inputs(tmp_path, HandoffExport(["fcp7_xml", "otio"], error="CommandError: boom"), missing=["/x/y.mp4"]))
    assert "Interchange export FAILED" in text and "boom" in text and "UNVERIFIED" in text and "/x/y.mp4" in text
    assert "absolute" in text and "docs/resolve-verification.md" in text and "tools/resolve_verify.py" in text


def test_report_orders_fcp7_first_and_warns_about_dangerous_formats(tmp_path):
    rep = HandoffReport(str(tmp_path), [FormatReport("fcp7_xml", "xml", str(tmp_path / "timeline.xml"), "fallback", exported=True, ok=True),
                                        FormatReport("edl", "edl", str(tmp_path / "timeline.edl"), "last resort", exported=True, ok=True)],
                        limitations=[], filmcraft_version="0.13.0")
    text = render_handoff_report(_inputs(tmp_path, HandoffExport(["fcp7_xml", "edl"], rep, dangerous_requested=["edl"])))
    assert text.index("| fcp7_xml | PRIMARY |") < text.index("| edl | secondary |")
    assert "verified to import in Resolve 21.1 (Phase 0 fixture)" in text and "froze Resolve 21.1" in text
    assert "WARNING: unsafe formats" in text and "observed on Resolve 21.1 with the Phase 0 fixture" in text
    assert "| fcp7_xml | PRIMARY |" in text


def test_report_claims_verification_only_when_passing(tmp_path):
    ex = HandoffExport([], HandoffReport(str(tmp_path), [], limitations=[]))
    assert "Verified by the user-run script" in render_handoff_report(_inputs(tmp_path, ex, ("passing", "status 'pass'")))
    assert "UNVERIFIED" in render_handoff_report(_inputs(tmp_path, ex, ("not_passing", "status 'fail'")))
