"""The critic end to end on the ffmpeg-generated fixtures: report and apply with a fake critic endpoint. The apply test needs $FILMCRAFT_CLI and
rebuilds a NEW versioned FilmCraft project, re-renders the preview and re-runs QC (skipped without FilmCraft); the gate tests need ffmpeg only."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import hash_tree
from critic_helpers import CriticTransport, change, finding, gateway_for

from trip_vlog import cli
from trip_vlog import critic_stages as cs
from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.execution import load_journal
from trip_vlog.filmcraft.preview import run_ffprobe
from trip_vlog.orchestrator import run_draft
from trip_vlog.planning.models import EditPlan

CLI = os.environ.get("FILMCRAFT_CLI")
needs_fc = pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")
pytestmark = pytest.mark.integration
ENDPOINT = "llm.endpoints={main: {api: anthropic_messages, model: test-model}}"


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def fc_sequence(ws: Path, media: Path, project: Path):
    ad = FilmCraftAdapter(ws, media, executable=CLI, request_timeout=120)
    try:
        ad.open_project(project)
        return ad.inspect_sequence()
    finally:
        ad.close()


@pytest.fixture
def fake_critic(monkeypatch):
    t = CriticTransport()
    monkeypatch.setattr(cs, "gateway_factory", lambda c: gateway_for(c.config, c.workspace.root, t))
    return t


def trim_and_dissolve(call):
    """clip_002 (the portrait video, a whole file with no spare source) is trimmed, which frees the source a centred dissolve into the still needs."""
    first, second, third = call.clips[0], call.clips[1], call.clips[2]
    tile = next(tid for tid, _, state in call.tiles if state == "image supplied")
    return {"summary": "The portrait clip drags and the change to the photo is abrupt.", "findings": [
        finding(severity="major", category="pacing", summary="The portrait clip lingers.", clips=[second],
                evidence=[{"kind": "plan", "ref": second, "note": "4 s for one view"}, {"kind": "tile", "ref": tile, "note": "the first frame"}],
                proposed=change("trim_clip", clip_instance_id=second, duration_seconds=3.0)),
        finding(severity="minor", category="story_clarity", summary="Soften the change to the photo.", clips=[second, third],
                evidence=[{"kind": "qc_check", "ref": "orientation", "note": "portrait in a landscape sequence"}],
                proposed=change("set_transition", clip_instance_id=third, transition_type="dissolve", transition_seconds=0.5))]}


@pytest.fixture
def media(tmp_path, generated_media) -> Path:
    m = tmp_path / "media"
    shutil.copytree(generated_media, m)
    return m


def test_critic_command_needs_a_draft(tmp_path, capsys):
    code, out, err = run(capsys, "critic", "--output", tmp_path / "nothing")
    assert code == 2 and "holds no draft" in err


def test_the_default_draft_has_no_critic_stage_and_no_report(media, tmp_path, ffmpeg_tools):
    ws = tmp_path / "ws"
    res = run_draft(media, ws, None, {"project.target_duration_seconds": 12}, echo=lambda *_: None)
    assert "critic" not in res.stage_status and not (ws / "qc" / "critic_report.json").exists()


@needs_fc
def test_report_mode_in_a_draft_and_on_demand(media, tmp_path, fake_critic, capsys):
    fake_critic.respond = trim_and_dissolve
    ws = tmp_path / "ws"
    before = hash_tree(media)
    code, out, err = run(capsys, "draft", "--media", media, "--output", ws, "--duration", 12, "--auto-approve", "--filmcraft", CLI, "--set", ENDPOINT, "--set", "qc.critic=report")
    assert code == 0, out + err
    assert "critic (report): ok; 2 finding(s)" in out and "preview sheets sent as images" in out and "critic_report.md" in out
    man = rj(ws / "manifest.json")
    assert man["stages"]["critic"]["status"] == "succeeded" and man["stages"]["handoff"]["status"] in ("succeeded", "partial")
    assert len(fake_critic.calls) == 1 and fake_critic.calls[0].images >= 1
    rep = rj(ws / "qc" / "critic_report.json")
    assert rep["mode"] == "report" and [f["change"]["status"] for f in rep["findings"]] == ["valid", "valid"] and rep["would_change"]["diff"]["total_delta_seconds"] < 0
    assert rj(ws / "preview" / "qc_report.json")["critic"]["status"] == "ok"
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj"]   # report mode builds nothing
    assert "critic report: qc/critic_report.md" in (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")
    # nothing changed: resume is all cache hits and makes no request
    code, out, _ = run(capsys, "resume", "--output", ws)
    assert code == 0 and len(fake_critic.calls) == 1 and rj(ws / "manifest.json")["stages"]["critic"]["decision"] == "cache_hit"
    # on demand, report mode again does not rewrite project.yaml
    yaml_before = (ws / "project.yaml").read_bytes()
    code, out, _ = run(capsys, "critic", "--output", ws, "--mode", "report", "--set", ENDPOINT)
    assert code == 0 and (ws / "project.yaml").read_bytes() == yaml_before and len(fake_critic.calls) == 1
    assert hash_tree(media) == before


@needs_fc
def test_apply_builds_a_new_version_rerenders_reruns_qc_and_hands_off_the_latest(media, tmp_path, fake_critic, capsys):
    ws = tmp_path / "ws"
    before = hash_tree(media)
    code, out, err = run(capsys, "draft", "--media", media, "--output", ws, "--duration", 12, "--auto-approve", "--filmcraft", CLI, "--set", ENDPOINT)
    assert code == 0, out + err
    assert "critic" not in rj(ws / "manifest.json")["stages"]
    v1 = ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj"
    v1_bytes = v1.read_bytes()
    plan1 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    preview1 = run_ffprobe(ws / "preview" / "draft.mp4")
    assert abs(float(preview1["format"]["duration"]) - 12) < 0.2
    yaml_before = (ws / "project.yaml").read_bytes()

    fake_critic.respond = trim_and_dissolve
    code, out, err = run(capsys, "critic", "--output", ws, "--mode", "apply", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0, out + err
    assert (ws / "project.yaml").read_bytes() == yaml_before                      # a one-off: the config on disk is unchanged
    assert "critic (apply): ok" in out and "change(s) applied" in out and "earlier versions are kept" in out
    man = rj(ws / "manifest.json")

    # a NEW versioned project; the old one is untouched and still there
    projects = sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj"))
    assert projects == ["Trip Vlog Draft_v001.fcproj", "Trip Vlog Draft_v002.fcproj"] and v1.read_bytes() == v1_bytes
    runs = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"]
    assert [(r["version"], r["status"], r["verified_after_reopen"]) for r in runs] == [(1, "complete", True), (2, "complete", True)]
    assert man["filmcraft"]["project"].endswith("_v002.fcproj") and man["filmcraft"]["project_version"] == 2

    # the new plan: trimmed first clip, dissolve into the second, hash and approval chain
    plan2 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert plan2.plan_hash != plan1.plan_hash
    c2, c3 = plan2.scenes[0].clips[1], plan2.scenes[0].clips[2]
    assert c2.duration_frames == 90 and c3.transition_in.type == "dissolve" and c3.transition_in.duration_frames == 15 and c3.transition_in.alignment == "center"
    assert plan2.total_duration_frames == plan1.total_duration_frames - 30
    ap = man["approval"]
    assert ap["mode"] == "critic_fix" and ap["plan_hash"] == plan2.plan_hash and ap["parent_plan_hash"] == plan1.plan_hash and ap["parent_approval"]["mode"] == "automatic"
    rev = rj(ws / "plans" / "revisions" / "critic_r01.json")
    assert rev["status"] == "applied" and rev["plan_hash"] == plan2.plan_hash and rev["project"].endswith("_v002.fcproj") and rev["diff"]["total_delta_seconds"] == -1.0
    assert (ws / "plans" / "revisions" / "critic_r01.md").is_file() and (ws / "plans" / "revisions" / "critic_r01.parent_edit_plan.json").is_file()
    assert EditPlan.model_validate(rj(ws / "plans" / "revisions" / "critic_r01.parent_edit_plan.json")).plan_hash == plan1.plan_hash

    # reopened in a separate FilmCraft process: the new timeline is what the plan says
    seq = fc_sequence(ws, media, ws / man["filmcraft"]["project"])
    assert [c.end_frame - c.start_frame for c in seq.track("V1").clips][:2] == [150, 90] and len(seq.transitions) == 1
    assert seq.duration_frames == plan2.total_duration_frames
    old_seq = fc_sequence(ws, media, v1)
    assert old_seq.duration_frames == plan1.total_duration_frames and len(old_seq.transitions) == 0

    # preview re-rendered, QC re-run on the new version, critic block and handoff point at it
    preview2 = run_ffprobe(ws / "preview" / "draft.mp4")
    assert abs(float(preview2["format"]["duration"]) - plan2.total_duration_seconds) < 0.2 and float(preview2["format"]["duration"]) < float(preview1["format"]["duration"]) - 0.8
    assert (ws / "preview" / "draft.before_critic_r01.mp4").is_file()
    qc = rj(ws / "preview" / "qc_report.json")
    assert qc["plan_hash"] == plan2.plan_hash and qc["critic"]["status"] == "ok" and qc["critic"]["rounds"] == 1 and qc["status"] in ("pass", "warn")
    assert {c["name"]: c["status"] for c in qc["checks"]}["project_reopens"] == "pass"
    assert rj(ws / "plans" / "revisions" / "critic_r01.parent_qc_report.json")["plan_hash"] == plan1.plan_hash
    idx = rj(ws / "qc" / "preview_sheet.json")
    assert idx["plan_hash"] == plan2.plan_hash
    assert rj(ws / "resolve_handoff" / "project_reference.json")["project_version"] == 2
    assert rj(ws / "resolve_handoff" / "edit_plan.json")["plan_hash"] == plan2.plan_hash
    report = (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")
    assert "project version v2" in report and "critic_r01" in report and "critic report: qc/critic_report.md" in report
    rep = rj(ws / "qc" / "critic_report.json")
    assert rep["plan_hash"] == plan2.plan_hash and rep["rounds"][0]["status"] == "applied" and len(fake_critic.calls) == 2 and rep["images"]["sent"]

    # status points at the latest version and the critic report
    code, out, _ = run(capsys, "status", "--output", ws)
    assert code == 0 and "critic fix of" in out and "critic    : 1 round(s) applied" in out and "_v002.fcproj" in out and "critic" in out

    # everything is cached now: another run makes no request, builds nothing, keeps two versions
    calls, journal = len(fake_critic.calls), (ws / "filmcraft" / "execution_journal.json").read_bytes()
    code, out, err = run(capsys, "critic", "--output", ws, "--mode", "apply", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0, out + err
    assert len(fake_critic.calls) == calls and (ws / "filmcraft" / "execution_journal.json").read_bytes() == journal
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == projects
    assert hash_tree(media) == before
