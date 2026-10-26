"""Phase 5a with real FilmCraft: preview sheets + index, the new QC checks, and the handoff compatibility report on a baseline draft
with a still and opening/closing titles. Needs $FILMCRAFT_CLI (skipped otherwise) and ffmpeg."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trip_vlog import cli
from trip_vlog.handoff.compatibility import REPORT_JSON, REPORT_MD
from trip_vlog.qc.preview import load_preview_sheet_index

CLI = os.environ.get("FILMCRAFT_CLI")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")]


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def drafted(tmp_path_factory, generated_media):
    import shutil

    root = tmp_path_factory.mktemp("compat")
    media, ws = root / "media", root / "ws"
    shutil.copytree(generated_media, media)
    code = cli.main(["draft", "--media", str(media), "--output", str(ws), "--auto-approve", "--duration", "12", "--filmcraft", CLI,
                     "--set", "editorial.opening_title=true", "--set", "editorial.closing_title=Thanks for watching", "--set", "project.title=Compat Trip"])
    assert code == 0
    return ws


def test_preview_sheet_index_maps_tiles_to_clips_and_scenes(drafted):
    idx = load_preview_sheet_index(drafted / "qc" / "preview_sheet.json")
    plan = rj(drafted / "plans" / "edit_plan.json")
    clips = [c for s in plan["scenes"] for c in s["clips"]]
    assert len(idx.cuts) == len(clips) - 1 and not idx.truncated
    sides = [t.side for t in idx.tiles.values()]
    assert sides.count("before") == sides.count("after") == len(clips) - 1 and "start" in sides and "end" in sides
    for cut in idx.cuts:
        b, a = idx.tiles[cut.before_tile], idx.tiles[cut.after_tile]
        assert b.clip_instance_id == cut.outgoing_clip_instance_id and a.clip_instance_id == cut.incoming_clip_instance_id
        assert b.record_seconds < cut.record_seconds < a.record_seconds and b.scene_id == cut.outgoing_scene_id and a.extracted and b.extracted
    assert all((drafted / s.path).stat().st_size > 1000 for s in idx.sheets)
    qc = rj(drafted / "preview" / "qc_report.json")
    st = {c["name"]: c["status"] for c in qc["checks"]}
    assert "fail" not in st.values() and st["preview_decodes"] == "pass" and qc["contact_sheet"]["index"] == "qc/preview_sheet.json"
    assert {"speech_truncation", "audio_discontinuity", "music_vs_speech", "scene_order"} <= set(st)
    assert (drafted / "preview" / "qc_report.md").read_text(encoding="utf-8").startswith("# QC report")


def test_compatibility_report_rows_and_xml_round_trip(drafted):
    ho = drafted / "resolve_handoff"
    doc = rj(ho / REPORT_JSON)
    assert (ho / REPORT_MD).is_file() and doc["xml"]["status"] == "ok"
    rt = doc["xml"]["roundtrip"]
    assert rt["ok"] and rt["mismatches"] == [] and rt["tolerance_frames"] == 0       # recorded: every clip, title and marker matches to the frame
    rows = {r["feature"]: r for r in doc["features"]}
    assert {"timeline_settings", "cuts", "stills", "titles", "markers"} <= set(rows)
    assert rows["titles"]["plan_count"] == 2 and rows["titles"]["xml"]["matched"] == 2 and rows["titles"]["verdict"] == "manual"
    assert rows["stills"]["plan_count"] == 1 and rows["stills"]["verdict"] == "manual" and "resolve-finish" in rows["stills"]["instruction"]
    assert rows["cuts"]["verdict"] == "verified" and rows["markers"]["verdict"] == "verified"
    assert all(r["filmcraft"]["status"] == "supported" and r["xml"]["status"] == "ok" for r in doc["features"])
    assert doc["resolve_finish_report"]["used"] is False and doc["resolve_evidence"]["resolve"]["version"] == "21.1.1.10"
    summ = rj(drafted / "manifest.json")["stages"]["handoff"]["summary"]
    assert summ["compatibility"]["xml_roundtrip_ok"] is True and summ["compatibility_report"] == "resolve_handoff/compatibility_report.json"
    assert "compatibility_report.md" in (ho / "handoff_report.md").read_text(encoding="utf-8")


def test_status_points_to_the_compatibility_report(drafted, capsys):
    assert cli.main(["status", "--output", str(drafted)]) == 0
    assert "compatibility_report.md" in capsys.readouterr().out
