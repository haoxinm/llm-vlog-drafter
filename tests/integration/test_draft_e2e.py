"""End-to-end acceptance (DESIGN §19.3, §20 Phase 2 exit): fixture folder -> draft -> preview -> QC -> Resolve handoff.

Gate/validation tests need ffmpeg only. Tests that build a FilmCraft project also need $FILMCRAFT_CLI (skipped otherwise).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import hash_tree
from jsonschema import Draft202012Validator

from trip_vlog import cli
from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.execution import load_journal
from trip_vlog.filmcraft.preview import run_ffprobe
from trip_vlog.orchestrator import run_draft
from trip_vlog.planning.models import json_schema

CLI = os.environ.get("FILMCRAFT_CLI")
needs_fc = pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")
pytestmark = pytest.mark.integration
DURATION = 12  # fixture media: 5 s landscape + 4 s portrait + one still (3 s) = exactly 12 s


def rj(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def draft(media: Path, ws: Path, **ov):
    overrides = {"project.target_duration_seconds": DURATION, **ov}
    return run_draft(media, ws, None, overrides, echo=lambda *_: None)


def fc_sequence(ws: Path, media: Path, project: Path):
    ad = FilmCraftAdapter(ws, media, executable=CLI, request_timeout=120)
    try:
        ad.open_project(project)
        return ad.inspect_sequence()
    finally:
        ad.close()


@pytest.fixture(scope="module")
def media(tmp_path_factory, generated_media) -> Path:
    m = tmp_path_factory.mktemp("e2e") / "media"
    shutil.copytree(generated_media, m)
    (m / "corrupt.mp4").write_bytes(b"this is not a video" * 50)
    return m


@pytest.fixture(scope="module")
def drafted(tmp_path_factory, media):
    if not CLI or not Path(CLI).is_file():
        pytest.skip("$FILMCRAFT_CLI not set or not a file")
    before = hash_tree(media)
    ws = tmp_path_factory.mktemp("e2e_ws") / "ws"
    res = draft(media, ws, **{"workflow.approval": "auto_approve", "filmcraft.executable": CLI})
    return {"ws": ws, "res": res, "before": before}


# ---- gate and validation: no FilmCraft needed ------------------------------------------------------------------

def test_default_stops_at_awaiting_approval_before_any_filmcraft_change(tmp_path, media, ffmpeg_tools, capsys):
    ws = tmp_path / "ws"
    res = draft(media, ws)
    assert res.awaiting_approval and res.exit_code == 3
    assert res.stage_status["plan"] == "succeeded" and res.stage_status["approval"] == "awaiting_approval"
    assert all(res.stage_status[s] == "skipped" for s in ("assemble", "preview", "qc", "handoff"))
    assert not list(ws.rglob("*.fcproj")) and not (ws / "filmcraft").exists()
    m = rj(ws / "manifest.json")
    assert m["approval"]["status"] == "pending" and m["planner"] == "baseline"
    assert (ws / "plans" / "plan_summary.md").is_file()
    assert cli.main(["status", "--output", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "awaiting_approval" in out and "assemble" in out and "pending" in out and "trip-vlog approve" in out


def test_corrupt_file_is_reported_and_not_planned(tmp_path, media, ffmpeg_tools):
    ws = tmp_path / "ws"
    draft(media, ws)
    plan = rj(ws / "plans" / "edit_plan.json")
    assets = {c["asset_id"] for s in plan["scenes"] for c in s["clips"]}
    assert len(assets) == 3
    inv = rj(ws / "media_index" / "inventory_report.json")
    assert [f["path"] for f in inv["failed_probes"]] == ["corrupt.mp4"]


def test_invalid_plan_is_rejected_before_filmcraft(tmp_path, media, ffmpeg_tools):
    ws = tmp_path / "ws"
    assert draft(media, ws).awaiting_approval
    p = ws / "plans" / "edit_plan.json"
    base = rj(p)
    for label, edit in (("bad id", lambda d: d["scenes"][0]["clips"][0].update(asset_id="asset_0123456789abcdef")),
                        ("out of range", lambda d: d["scenes"][0]["clips"][0].update(source_out_seconds=99.0))):
        doc = json.loads(json.dumps(base))
        edit(doc)
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert cli.main(["approve", "--output", str(ws)]) == 4, label  # approve refuses an invalid plan
        res = draft(media, ws, **{"workflow.approval": "auto_approve"})  # auto-approve still cannot get past validation
        assert res.stage_status["assemble"] == "failed" and res.exit_code == 1, label
        err = rj(ws / "manifest.json")["stages"]["assemble"]["error"]
        assert "rejected before any FilmCraft change" in err, label
        assert not (ws / "filmcraft").exists() and not list(ws.rglob("*.fcproj")), label
    assert rj(ws / "plans" / "assemble_validation_report.json")["ok"] is False


def test_approval_is_bound_to_plan_hash(tmp_path, media, ffmpeg_tools):
    ws = tmp_path / "ws"
    assert draft(media, ws).awaiting_approval
    assert cli.main(["approve", "--output", str(ws)]) == 0
    m = rj(ws / "manifest.json")
    assert m["approval"]["mode"] == "manual" and m["approval"]["plan_hash"] == rj(ws / "plans" / "edit_plan.json")["plan_hash"]
    res = draft(media, ws, **{"project.target_duration_seconds": 9, "filmcraft.executable": CLI or "nonexistent"})
    assert res.awaiting_approval  # new plan -> approval invalidated
    assert rj(ws / "manifest.json")["approval"]["status"] == "invalidated"


# ---- full pipeline with real FilmCraft -------------------------------------------------------------------------

@needs_fc
def test_full_draft_outputs(drafted, media):
    ws, res = drafted["ws"], drafted["res"]
    assert res.exit_code == 0, res.stage_status
    assert res.stage_status == {s: "succeeded" for s in ("scan", "probe", "catalog", "thumbnails", "segment", "plan", "approval", "assemble", "preview", "qc", "handoff")} \
        or all(v in ("succeeded", "partial") for v in res.stage_status.values())
    m = rj(ws / "manifest.json")
    assert m["approval"]["mode"] == "automatic" and m["planner"] == "baseline"
    assert m["filmcraft"]["version"] and (ws / m["filmcraft"]["capability_snapshot"]).is_file()
    ids = [a["asset_id"] for a in map(json.loads, (ws / "media_index" / "assets.jsonl").read_text(encoding="utf-8").splitlines())]
    assert len(ids) == 6 and all(i.startswith("asset_") and len(i) == 22 for i in ids)
    plan = rj(ws / "plans" / "edit_plan.json")
    assert not list(Draft202012Validator(json_schema("edit_plan")).iter_errors(plan))
    proj = ws / m["filmcraft"]["project"]
    assert proj.suffix == ".fcproj" and proj.is_file()
    run = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"][0]
    assert run["status"] == "complete" and run["verified_after_reopen"]
    seq = fc_sequence(ws, media, proj)
    assert [len(t.clips) for t in seq.tracks if t.name in ("V1", "A1")] == [3, 2]
    probe = run_ffprobe(ws / "preview" / "draft.mp4")
    assert abs(float(probe["format"]["duration"]) - DURATION) < 0.2 and any(s["codec_type"] == "audio" for s in probe["streams"])
    qc = rj(ws / "preview" / "qc_report.json")
    st = {c["name"]: c["status"] for c in qc["checks"]}
    assert qc["status"] in ("pass", "warn") and "fail" not in st.values(), st
    assert st["project_reopens"] == st["no_v1_gaps"] == st["preview_playable"] == st["total_duration"] == "pass"
    assert st["orientation"] == "warn"  # the portrait fixture in a landscape sequence
    assert (ws / "preview" / "contact_sheet_timeline.jpg").stat().st_size > 1000
    ho = ws / "resolve_handoff"
    for name in ("timeline.xml", "timeline.otio", "media_manifest.csv", "edit_plan.json", "handoff_report.md", "project_reference.json"):
        assert (ho / name).is_file(), name
    rows = list(csv.DictReader((ho / "media_manifest.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == 6 and sum(r["used_in_timeline"] == "yes" for r in rows) == 3
    assert all(Path(r["absolute_path"]).is_absolute() for r in rows)
    assert next(r for r in rows if r["relative_path"] == "corrupt.mp4")["probe_status"] == "failed"
    assert rj(ho / "edit_plan.json")["plan_hash"] == plan["plan_hash"]
    report = (ho / "handoff_report.md").read_text(encoding="utf-8")
    assert "UNVERIFIED" in report and "docs/resolve-verification.md" in report and report.index("fcp7_xml") < report.index("otio")
    assert hash_tree(media) == drafted["before"]  # originals untouched


@needs_fc
def test_rerun_on_unchanged_inputs_is_a_noop_for_assemble(drafted, media):
    ws = drafted["ws"]
    jbefore = (ws / "filmcraft" / "execution_journal.json").read_bytes()
    projects = sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj"))
    res = draft(media, ws)
    assert res.exit_code == 0
    m = rj(ws / "manifest.json")["stages"]
    assert all(m[s]["decision"] == "cache_hit" for s in ("segment", "plan", "assemble", "preview", "qc", "handoff"))
    assert m["probe"]["summary"]["probed"] == 1  # only the corrupt file is retried; everything else comes from the cache
    assert (ws / "filmcraft" / "execution_journal.json").read_bytes() == jbefore
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == projects == ["Trip Vlog Draft_v001.fcproj"]


@needs_fc
def test_changed_duration_replans_and_versions_without_reprobing(drafted, media, tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(drafted["ws"], ws)
    res = draft(media, ws, **{"project.target_duration_seconds": 8})
    assert res.exit_code == 0, res.stage_status
    m = rj(ws / "manifest.json")["stages"]
    assert m["probe"]["summary"]["probed"] == 1 and m["segment"]["decision"] == "cache_hit"  # probe retries only the corrupt file
    assert m["plan"]["decision"] == "ran" and m["assemble"]["decision"] == "ran" and m["assemble"]["summary"]["version"] == 2
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj", "Trip Vlog Draft_v002.fcproj"]
    plan = rj(ws / "plans" / "edit_plan.json")
    assert abs(plan["total_duration_seconds"] - 8) <= 2
    seq = fc_sequence(ws, media, ws / "filmcraft" / "Trip Vlog Draft_v002.fcproj")
    assert abs(float(seq.duration_frames / seq.fps) - plan["total_duration_seconds"]) < 0.1


@needs_fc
def test_default_flow_approve_then_resume_finishes(tmp_path, media):
    ws = tmp_path / "ws"
    first = draft(media, ws, **{"filmcraft.executable": CLI})
    assert first.awaiting_approval and not list(ws.rglob("*.fcproj"))
    assert cli.main(["approve", "--output", str(ws)]) == 0
    assert cli.main(["resume", "--output", str(ws)]) == 0
    assert len(list(ws.rglob("*.fcproj"))) == 1 and (ws / "preview" / "qc_report.json").is_file()
    m = rj(ws / "manifest.json")
    assert m["approval"]["mode"] == "manual" and m["stages"]["handoff"]["status"] == "succeeded"
    assert cli.main(["status", "--output", str(ws)]) == 0


@needs_fc
def test_interrupted_assemble_resumes_without_duplicate_clips(tmp_path, media, monkeypatch):
    ws = tmp_path / "ws"
    orig = FilmCraftAdapter.create_sequence

    def crash(self, settings):
        raise RuntimeError("simulated crash after import")

    monkeypatch.setattr(FilmCraftAdapter, "create_sequence", crash)
    res = draft(media, ws, **{"workflow.approval": "auto_approve", "filmcraft.executable": CLI})
    assert res.exit_code == 1 and res.stage_status["assemble"] == "failed"
    assert res.stage_status["preview"] == res.stage_status["qc"] == res.stage_status["handoff"] == "skipped"
    runs = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"]
    assert [r["status"] for r in runs] == ["failed"] and "import_media" in runs[0]["completed_ops"][1]
    monkeypatch.setattr(FilmCraftAdapter, "create_sequence", orig)
    assert cli.main(["resume", "--output", str(ws)]) == 0
    runs = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"]
    assert [(r["version"], r["status"]) for r in runs] == [(1, "failed"), (2, "complete")]
    seq = fc_sequence(ws, media, ws / runs[1]["project_path"])
    assert [len(t.clips) for t in seq.tracks if t.name in ("V1", "A1")] == [3, 2]
    names = [c.name for c in seq.track("V1").clips]
    assert len(names) == len(set(names)) == 3
    ad = FilmCraftAdapter(ws, media, executable=CLI, request_timeout=120)
    try:  # the interrupted version crashed before creating a sequence, so it holds no clips at all
        assert ad.open_project(ws / runs[0]["project_path"]).sequences() == []
    finally:
        ad.close()
    assert rj(ws / "preview" / "qc_report.json")["status"] in ("pass", "warn")


@needs_fc
def test_failed_assembly_is_marked_listed_and_kept_out_of_resume_and_handoff(tmp_path, media, monkeypatch, capsys):
    from trip_vlog import draft_stages as ds

    ws = tmp_path / "ws"
    before = hash_tree(media)
    orig = FilmCraftAdapter.create_sequence
    monkeypatch.setattr(FilmCraftAdapter, "create_sequence", lambda self, settings: (_ for _ in ()).throw(RuntimeError("simulated crash after import")))
    res = draft(media, ws, **{"workflow.approval": "auto_approve", "filmcraft.executable": CLI})
    assert res.exit_code == 1 and res.stage_status["assemble"] == "failed"
    m = rj(ws / "manifest.json")
    (fv,) = m["filmcraft"]["failed_versions"]
    assert fv["version"] == 1 and fv["status"] == "failed" and fv["failed_at_op"] == "create_sequence" and "simulated crash" in fv["error"]
    assert m["stages"]["assemble"]["summary"]["failed_version"] == 1
    partial = ws / fv["project"]
    assert partial.is_file(), "the partial project is kept for inspection"
    assert not (ws / "resolve_handoff").exists() or not (ws / "resolve_handoff" / "handoff_report.md").exists()
    assert cli.main(["status", "--output", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "failed build v1" in out and "kept but not used" in out
    monkeypatch.setattr(FilmCraftAdapter, "create_sequence", orig)
    assert cli.main(["resume", "--output", str(ws)]) == 0
    partial_bytes = partial.read_bytes()
    m = rj(ws / "manifest.json")
    assert m["filmcraft"]["project_version"] == 2 and [f["version"] for f in m["filmcraft"]["failed_versions"]] == [1]
    assert partial.read_bytes() == partial_bytes and hash_tree(media) == before
    ref = rj(ws / "resolve_handoff" / "project_reference.json")
    assert ref["project_version"] == 2
    assert "failed FilmCraft build(s) v1 (failed)" in (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj", "Trip Vlog Draft_v002.fcproj"]
