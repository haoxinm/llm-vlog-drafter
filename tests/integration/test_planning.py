"""Segment + baseline plan against ffmpeg-generated fixtures (skipped cleanly when ffmpeg is missing)."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import hash_tree
from jsonschema import Draft202012Validator

from trip_vlog.config import load_config, resolve_paths
from trip_vlog.logging_utils import RunLogger
from trip_vlog.media.catalog import Catalog
from trip_vlog.media.segment import SegmentSettings, build_segments, detect_asset, segment_assets, segment_inputs
from trip_vlog.media.tools import find_ffmpeg, find_ffprobe
from trip_vlog.orchestrator import RunContext, build_index_runner, run_index
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.models import EditPlan, SelectionPlan, json_schema
from trip_vlog.planning.pipeline import plan_baseline, plan_baseline_stage, plan_inputs
from trip_vlog.planning.validate import ValidationSettings, validate_edit_plan
from trip_vlog.state import Manifest, Workspace

pytestmark = pytest.mark.integration
OVERRIDES = {"project.target_duration_seconds": 12}


def quiet_index(media, ws, overrides=None):
    return run_index(media, ws, None, overrides or OVERRIDES, echo=lambda *_: None)


def make_ctx(media: Path, ws: Path) -> RunContext:
    cfg = load_config(ws / "project.yaml")
    w = Workspace(ws)
    return RunContext(cfg, resolve_paths(cfg, media, ws), w, RunLogger(w.run_log, w.errors_log, "test-run"), Manifest.load(w.manifest_path),
                      Catalog(w.catalog_path), find_ffprobe(), find_ffmpeg())


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cut_video(tmp_path_factory, ffmpeg_tools) -> Path:
    """12 s, 30 fps: hard cut at 4 s (testsrc2 -> solid red), tone with silence from 6 s to 9 s."""
    out = tmp_path_factory.mktemp("cutvideo") / "cuts.mp4"
    subprocess.run([ffmpeg_tools[0], "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=enable='between(t,6,9)':volume=0[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
                   check=True, capture_output=True)
    return out


def test_ffmpeg_detection_finds_cut_silence_and_levels(cut_video, ffmpeg_tools):
    s = SegmentSettings(window_seconds=6.0, min_segment_seconds=1.5)
    det = detect_asset(Path(ffmpeg_tools[0]), cut_video, "video", 1, 12.0, s)
    assert any(abs(t - 4.0) < 0.1 and score > 0.3 for t, score in det["cuts"]), det["cuts"]
    assert any(abs(a - 6.0) < 0.3 and abs(b - 9.0) < 0.3 for a, b in det["silences"]), det["silences"]
    assert det["has_audio"] and len(det["windows"]) >= 40
    segs = build_segments("asset_0123456789abcdef", "video", 12.0, Fraction(30), det, s)
    assert [(x["start_frame"], x["end_frame"]) for x in segs] == [(0, 120), (120, 180), (180, 270), (270, 360)]
    assert [x["features"]["silent"] for x in segs] == [False, False, True, False]
    assert segs[1]["features"]["scene_cut_score"] > 0.3
    again = build_segments("asset_0123456789abcdef", "video", 12.0, Fraction(30), det, s)
    assert [x["segment_id"] for x in again] == [x["segment_id"] for x in segs]


def test_segment_stage_persists_caches_and_exports(media_dir, workspace):
    quiet_index(media_dir, workspace)
    ctx = make_ctx(media_dir, workspace)
    try:
        res = segment_assets(ctx)
        assert res.status == "succeeded" and res.summary["segments"] == 5 and res.summary["detection_cache_hits"] == 0
        segs = {r["asset_id"]: json.loads(r["data_json"]) for r in ctx.catalog.list_segments()}
        assert len(segs) == 5
        still = [s for s in segs.values() if s["media_type"] == "image"][0]
        assert still["start_seconds"] is None and still["features"] == {"still": True}
        land = [s for s in segs.values() if s["media_type"] == "video" and s["fps"] == "30" and s["end_frame"] == 150][0]
        assert land["features"]["silent"] is False and land["features"]["audio_mean_db"] < -10
        moments = ctx.catalog.list_moments()
        assert len(moments) == 5 and all(m["segment_id"] for m in moments)
        lines = (workspace / "media_index" / "moments.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5 and {json.loads(l)["moment_id"] for l in lines} == {m["moment_id"] for m in moments}
        ids = sorted(r["segment_id"] for r in ctx.catalog.list_segments())
        # second run: detection comes from the cache, same IDs
        res2 = segment_assets(ctx)
        assert res2.summary["detection_cache_hits"] == 4 and sorted(r["segment_id"] for r in ctx.catalog.list_segments()) == ids
        # a smaller window re-combines cached detections into more segments without re-running ffmpeg
        ctx.config.media.segment_window_seconds = 2.0
        res3 = segment_assets(ctx)
        assert res3.summary["detection_cache_hits"] == 4 and res3.summary["segments"] > 5
        ctx.config.media.segment_window_seconds = 6.0
        assert segment_inputs(ctx) != {}
    finally:
        ctx.catalog.close()


def test_changed_file_invalidates_detection_cache(media_dir, workspace):
    quiet_index(media_dir, workspace)
    ctx = make_ctx(media_dir, workspace)
    try:
        segment_assets(ctx)
    finally:
        ctx.catalog.close()
    target = media_dir / "landscape_5s.mp4"
    shutil.copy(media_dir / "portrait_4s.mp4", target)  # different content under the same path
    quiet_index(media_dir, workspace)
    ctx = make_ctx(media_dir, workspace)
    try:
        res = segment_assets(ctx)
        assert res.summary["detection_cache_hits"] == 3  # the changed video was re-detected
        row = [r for r in ctx.catalog.list_assets() if r["relative_path"] == "landscape_5s.mp4"][0]
        [seg] = [json.loads(r["data_json"]) for r in ctx.catalog.list_segments(row["asset_id"])]
        assert seg["end_frame"] == 120  # now 4 s long
    finally:
        ctx.catalog.close()


def test_plan_baseline_end_to_end(media_dir, workspace):
    before = hash_tree(media_dir)
    quiet_index(media_dir, workspace)
    ctx = make_ctx(media_dir, workspace)
    try:
        assert segment_assets(ctx).status == "succeeded"
        selection, edit, report = plan_baseline(ctx)
    finally:
        ctx.catalog.close()
    assert report.ok, report.format()
    assert hash_tree(media_dir) == before  # originals untouched
    plans = workspace / "plans"
    for name in ("selection_plan.json", "edit_plan.json", "selection_plan.schema.json", "edit_plan.schema.json", "plan_summary.md", "validation_report.json"):
        assert (plans / name).is_file(), name
    sel_doc, edit_doc = read_json(plans / "selection_plan.json"), read_json(plans / "edit_plan.json")
    assert list(Draft202012Validator(read_json(plans / "selection_plan.schema.json")).iter_errors(sel_doc)) == []
    assert list(Draft202012Validator(read_json(plans / "edit_plan.schema.json")).iter_errors(edit_doc)) == []
    assert read_json(plans / "edit_plan.schema.json") == json_schema("edit_plan")
    SelectionPlan.model_validate(sel_doc)
    plan = EditPlan.model_validate(edit_doc)
    assert plan.total_duration_seconds == pytest.approx(12.0, abs=2.0)
    kinds = [(c.media_type, c.duration_frames) for s in plan.scenes for c in s.clips]
    assert kinds == [("video", 150), ("video", 120), ("image", 90)]  # landscape 5 s, portrait 4 s, still 3 s; audio-only not placed
    assert (plans / "plan_summary.md").read_text(encoding="utf-8").startswith("# Trip Vlog: edit plan summary")
    assert b"\r" not in (plans / "edit_plan.json").read_bytes()
    # re-validate from disk against the catalog
    with Catalog(workspace / "media_index" / "catalog.sqlite") as cat:
        view = PlanningCatalog.from_catalog(cat)
        r = validate_edit_plan(edit_doc, view, ValidationSettings.from_config(load_config(workspace / "project.yaml")))
        assert r.ok and r.repairs == []
        # deliberately invalid IDs and out-of-range source times are rejected (DESIGN 19.3 item 3)
        bad = json.loads(json.dumps(edit_doc))
        bad["scenes"][0]["clips"][0]["moment_id"] = "moment_deadbeefdeadbeef"
        bad["scenes"][0]["clips"][1]["asset_id"] = "asset_ffffffffffffffff"
        bad["scenes"][0]["clips"][0]["source_out_seconds"] = 99.0
        bad["scenes"][0]["clips"][0]["source_out_frame"] = 2970
        r = validate_edit_plan(bad, view, ValidationSettings.from_config(load_config(workspace / "project.yaml")))
        assert not r.ok and {"moment_not_found", "asset_not_found", "source_out_of_range"} <= set(r.codes())


def test_plan_is_identical_across_workspaces(media_dir, tmp_path):
    outs = []
    for name in ("w1", "w2"):
        ws = tmp_path / name
        quiet_index(media_dir, ws)
        ctx = make_ctx(media_dir, ws)
        try:
            segment_assets(ctx)
            plan_baseline(ctx)
        finally:
            ctx.catalog.close()
        outs.append(((ws / "plans" / "edit_plan.json").read_bytes(), (ws / "plans" / "selection_plan.json").read_bytes()))
    assert outs[0] == outs[1]


def test_stages_register_on_runner_and_cache(media_dir, workspace):
    quiet_index(media_dir, workspace)
    ctx = make_ctx(media_dir, workspace)
    try:
        def build():
            runner = build_index_runner(ctx)
            runner.register("segment", segment_assets, deps=("catalog",), inputs=segment_inputs)
            runner.register("plan", plan_baseline_stage, deps=("segment",), inputs=plan_inputs)
            return runner
        st = build().run()
        assert str(st["segment"]) == "succeeded" and str(st["plan"]) == "succeeded", st
        assert ctx.manifest.stage("plan")["summary"]["clips"] == 3 and ctx.manifest.stage("segment")["summary"]["segments"] == 5
        build().run()
        assert ctx.manifest.stage("segment")["decision"] == "cache_hit" and ctx.manifest.stage("plan")["decision"] == "cache_hit"
        # a config change that affects the plan re-runs only the plan stage
        ctx.config.project.target_duration_seconds = 8
        build().run()
        assert ctx.manifest.stage("segment")["decision"] == "cache_hit" and ctx.manifest.stage("plan")["decision"] == "ran"
        assert ctx.manifest.stage("plan")["summary"]["clips"] == 2
    finally:
        ctx.catalog.close()


def test_plan_stage_fails_on_invalid_plan(media_dir, workspace):
    quiet_index(media_dir, workspace, {"project.target_duration_seconds": 12, "editorial.must_exclude": ["landscape_5s.mp4", "portrait_4s.mp4"]})
    ctx = make_ctx(media_dir, workspace)
    try:
        segment_assets(ctx)
        res = plan_baseline_stage(ctx)
        # only the still remains: 3 s against a 12 s target is outside tolerance -> FAILED, never silently accepted
        assert res.status == "failed" and "total_duration_out_of_tolerance" in (res.error or "")
        assert (workspace / "plans" / "edit_plan.json").is_file()  # written for inspection
    finally:
        ctx.catalog.close()
