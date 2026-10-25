"""`analyze` end to end on the ffmpeg-generated fixtures with a fake HTTP transport (skipped without ffmpeg)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport, gateway_for
from conftest import hash_tree
from PIL import Image

from trip_vlog import cli
from trip_vlog.analysis import stage as st
from trip_vlog.media.catalog import Catalog
from trip_vlog.orchestrator import run_analyze

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(monkeypatch):
    tr = LabelingTransport(best=lambda sid, n: {"first_index": 1, "last_index": n - 1} if n >= 3 else None)
    st.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    yield tr
    st.gateway_factory = None


def analyze(media, ws, **kw):
    return run_analyze(media, ws, None, {"llm.endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}, **kw.pop("overrides", {})},
                       echo=lambda *_: None, **kw)


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_analyze_end_to_end_and_resume(media_dir, workspace, fake):
    before = hash_tree(media_dir)
    res = analyze(media_dir, workspace)
    assert res.exit_code == 0 and res.stage_status["analyze"] == "succeeded" and res.stage_status["segment"] == "succeeded"
    man = rj(workspace / "manifest.json")
    s = man["stages"]["analyze"]["summary"]
    assert man["pipeline"] == "analyze" and s["labeled"] == 3 and s["fallback_unlabeled"] == 0 and s["sampling_fps"] == 1.0
    with Catalog(workspace / "media_index" / "catalog.sqlite") as cat:
        rows = [json.loads(r["data_json"]) for r in cat.list_moments()]
        segs = {r["segment_id"]: json.loads(r["data_json"]) for r in cat.list_segments()}
    video = [m for m in rows if m["media_type"] == "video"]
    assert len(video) == 2 and sum(m["source"] == "llm" for m in rows) == 3 and len(rows) == 5  # + 2 audio baseline moments
    for m in video:
        seg = segs[m["segment_id"]]
        assert m["source"] == "llm" and seg["start_seconds"] <= m["source_in_seconds"] < m["source_out_seconds"] <= seg["end_seconds"]
        assert m["evidence"] and all((workspace / e["artifact"]).is_file() for e in m["evidence"])
        ts = [e["timestamp_seconds"] for e in m["evidence"]]
        assert ts == sorted(ts) and all(seg["start_seconds"] <= t < seg["end_seconds"] for t in ts)
        with Image.open(workspace / m["evidence"][0]["artifact"]) as im:                                   # autorotated, bounded size
            assert max(im.size) <= 1568
        assert (workspace / m["contact_sheet"]).is_file()
    still = [m for m in rows if m["media_type"] == "image"]
    assert len(still) == 1 and still[0]["source_in_seconds"] is None and still[0]["labeled"]
    jl = (workspace / "media_index" / "moments.jsonl").read_text(encoding="utf-8")
    assert len(jl.splitlines()) == len(rows) and "gps" not in jl.lower()
    calls = len(fake.calls)
    assert calls >= 2 and any(f'kind="filename"' in t for c in fake.calls for t in c.texts)
    # originals untouched; a re-run neither re-extracts frames nor calls the endpoint
    assert hash_tree(media_dir) == before
    frames = {p: p.stat().st_mtime_ns for p in (workspace / "media_index" / "analysis").rglob("*.jpg")}
    res2 = analyze(media_dir, workspace)
    assert res2.stage_status["analyze"] == "skipped" and len(fake.calls) == calls
    assert frames == {p: p.stat().st_mtime_ns for p in (workspace / "media_index" / "analysis").rglob("*.jpg")}
    # changing the sampling rate re-samples and re-labels the videos only
    res3 = analyze(media_dir, workspace, overrides={"media.vision_sampling_fps": 2.0})
    assert res3.stage_status["analyze"] == "succeeded" and len(fake.calls) > calls
    assert rj(workspace / "manifest.json")["cli_overrides"]["media.vision_sampling_fps"] == 2.0


def test_baseline_draft_planner_still_works_after_analyze(media_dir, workspace, fake):
    analyze(media_dir, workspace)
    from trip_vlog.orchestrator import run_draft

    res = run_draft(media_dir, workspace, None, {"project.target_duration_seconds": 12}, echo=lambda *_: None)
    assert res.stage_status["plan"] in ("succeeded", "skipped") and res.stage_status["approval"] == "awaiting_approval"


def test_cli_dry_run_and_status(media_dir, workspace, fake, capsys):
    cfg = workspace.parent / "project.yaml"
    cfg.write_text(json.dumps({"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}}}), encoding="utf-8")
    rc = cli.main(["analyze", "--media", str(media_dir), "--output", str(workspace), "--config", str(cfg), "--dry-run", "--sampling-fps", "2"])
    out = capsys.readouterr().out
    assert rc == 0 and "dry run" in out and "(remote)" in out and "requests" in out and "at 2 fps" in out
    assert fake.calls == [] and "analyze" not in rj(workspace / "manifest.json")["stages"]
    assert cli.main(["analyze", "--media", str(media_dir), "--output", str(workspace), "--config", str(cfg)]) == 0
    assert cli.main(["status", "--output", str(workspace)]) == 0
    status = capsys.readouterr().out
    assert "analyze" in status and "succeeded" in status
    assert cli.main(["analyze", "--media", str(media_dir), "--output", str(workspace), "--config", str(cfg), "--sampling-fps", "0"]) == 2
