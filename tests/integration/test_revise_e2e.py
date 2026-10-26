"""Natural-language revisions end to end on the ffmpeg-generated fixtures with a fake revision endpoint and the real FilmCraft CLI ($FILMCRAFT_CLI):
a direct revision, a dry run, a proposal that stops (exit 3), `approve --revision` then `resume` / `revise --apply`, new project versions, preview, QC,
handoff notes and status. The gate tests need ffmpeg only."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import hash_tree
from revise_helpers import ReviseTransport, answer, gateway_for, op, question

from trip_vlog import cli
from trip_vlog import revise_stages as rs
from trip_vlog.filmcraft.adapter import FilmCraftAdapter
from trip_vlog.filmcraft.execution import load_journal
from trip_vlog.filmcraft.preview import run_ffprobe
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
def media(tmp_path, generated_media) -> Path:
    m = tmp_path / "media"
    shutil.copytree(generated_media, m)
    return m


@pytest.fixture
def fake(monkeypatch):
    t = ReviseTransport()
    monkeypatch.setattr(rs, "gateway_factory", lambda c: gateway_for(c.config, c.workspace.root, t))
    return t


def test_revise_needs_a_draft_and_a_request(tmp_path, capsys):
    code, out, err = run(capsys, "revise", "--output", tmp_path / "nothing", "shorter")
    assert code == 2 and "holds no draft" in err


@needs_fc
def test_direct_revision_dry_run_proposal_approval_and_status(media, tmp_path, fake, capsys):
    ws = tmp_path / "ws"
    before = hash_tree(media)
    code, out, err = run(capsys, "draft", "--media", media, "--output", ws, "--duration", 12, "--auto-approve", "--filmcraft", CLI, "--set", ENDPOINT)
    assert code == 0, out + err
    assert "revise" not in rj(ws / "manifest.json")["stages"]
    v1 = ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj"
    v1_bytes = v1.read_bytes()
    plan1 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    ids = [c.clip_instance_id for c in plan1.scenes[0].clips]
    assert len(ids) == 3
    yaml_before = (ws / "project.yaml").read_bytes()
    preview1 = float(run_ffprobe(ws / "preview" / "draft.mp4")["format"]["duration"])

    # ---- a dry run shows the diff and changes nothing -------------------------------------------------------------
    fake.respond = lambda call: answer([op("trim_clip", clip_instance_id=call.clips[1], duration_seconds=3.0, part="make the portrait clip shorter"),
                                        op("set_clip_audio", clip_instance_id=call.clips[0], audio="mute", part="mute the first clip")], summary="Shorten the portrait clip and mute the first.")
    tree = hash_tree(ws)
    code, out, err = run(capsys, "revise", "--output", ws, "make the portrait clip shorter and mute the first clip", "--dry-run", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0, out + err
    assert "dry run: nothing was changed" in out and "c" in out and f"{ids[1]}: 4.00 s -> 3.00 s" in out and len(fake.calls) == 1
    assert EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json")).plan_hash == plan1.plan_hash and not (ws / "plans" / "revisions").exists()
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj"]

    # ---- the same request for real: applied on its own, the response cache answers --------------------------------------
    code, out, err = run(capsys, "revise", "--output", ws, "make the portrait clip shorter and mute the first clip", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0, out + err
    assert len(fake.calls) == 1, "the dry run's reply was reused from the response cache"
    assert "revision applied (automatic): 2 change(s)" in out and "earlier versions are kept" in out and "revise_r01.md" in out
    assert (ws / "project.yaml").read_bytes() == yaml_before
    man = rj(ws / "manifest.json")
    projects = sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj"))
    assert projects == ["Trip Vlog Draft_v001.fcproj", "Trip Vlog Draft_v002.fcproj"] and v1.read_bytes() == v1_bytes
    runs = load_journal(ws / "filmcraft" / "execution_journal.json")["runs"]
    assert [(r["version"], r["status"], r["verified_after_reopen"]) for r in runs] == [(1, "complete", True), (2, "complete", True)]
    plan2 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert plan2.plan_hash != plan1.plan_hash and plan2.total_duration_frames == plan1.total_duration_frames - 30
    assert plan2.scenes[0].clips[0].audio_policy == "muted" and plan2.scenes[0].clips[1].duration_frames == 90
    ap = man["approval"]
    assert ap["mode"] == "revision" and ap["plan_hash"] == plan2.plan_hash and ap["parent_plan_hash"] == plan1.plan_hash and ap["parent_approval"]["mode"] == "automatic"
    assert man["filmcraft"]["project"].endswith("_v002.fcproj") and man["stages"]["revise"]["status"] == "succeeded"
    seq = fc_sequence(ws, media, ws / man["filmcraft"]["project"])
    assert [c.end_frame - c.start_frame for c in seq.track("V1").clips] == [c.duration_frames for c in plan2.scenes[0].clips] and seq.duration_frames == plan2.total_duration_frames
    assert fc_sequence(ws, media, v1).duration_frames == plan1.total_duration_frames
    preview2 = float(run_ffprobe(ws / "preview" / "draft.mp4")["format"]["duration"])
    assert abs(preview2 - plan2.total_duration_seconds) < 0.2 and preview2 < preview1 - 0.8 and (ws / "preview" / "draft.before_revise_r01.mp4").is_file()
    qc = rj(ws / "preview" / "qc_report.json")
    assert qc["plan_hash"] == plan2.plan_hash and qc["status"] in ("pass", "warn") and {c["name"]: c["status"] for c in qc["checks"]}["project_reopens"] == "pass"
    doc = rj(ws / "plans" / "revisions" / "revise_r01.json")
    assert doc["status"] == "applied" and doc["project_version"] == 2 and doc["qc_before"] == "warn" and doc["duration"]["delta_seconds"] == -1.0 and len(doc["ops_applied"]) == 2
    assert EditPlan.model_validate(rj(ws / "plans" / "revisions" / "revise_r01.parent_edit_plan.json")).plan_hash == plan1.plan_hash
    handoff = (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")
    assert "project version v2" in handoff and "revise_r01" in handoff and "make the portrait clip shorter" in handoff
    assert rj(ws / "resolve_handoff" / "project_reference.json")["project_version"] == 2 and rj(ws / "resolve_handoff" / "edit_plan.json")["plan_hash"] == plan2.plan_hash

    # ---- repeating the command does not apply it twice -------------------------------------------------------------------
    code, out, err = run(capsys, "revise", "--output", ws, "make the portrait clip shorter and mute the first clip", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0 and "not applied twice" in out and EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json")).plan_hash == plan2.plan_hash
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == projects

    # ---- an unclear request is proposed: exit 3, nothing changes; approve, then resume applies exactly that -----------------
    ids2 = [c.clip_instance_id for c in plan2.scenes[0].clips]
    fake.respond = lambda call: answer([op("remove_clip", clip_instance_id=ids2[2], part="drop the photo")],
                                       ambiguous=[question("Do you mean the still photo at the end?", "the last picture", ids2[2])])
    code, out, err = run(capsys, "revise", "--output", ws, "drop the last picture", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 3, out + err
    assert "AWAITING CONFIRMATION of a revision" in out and "unclear: Do you mean the still photo at the end?" in out and "approve --revision" in out
    assert EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json")).plan_hash == plan2.plan_hash and len(list((ws / "filmcraft").glob("*.fcproj"))) == 2
    prop = rj(ws / "plans" / "revisions" / "revise_r02.json")
    assert prop["status"] == "proposed" and prop["authorization"] is None
    code, out, _ = run(capsys, "status", "--output", ws)
    assert code == 0 and "revise_r02 pending" in out and "approve --revision" in out and "revise_r01 applied" in out
    code, out, err = run(capsys, "resume", "--output", ws, "--filmcraft", CLI)
    assert code == 0 and len(list((ws / "filmcraft").glob("*.fcproj"))) == 2 and "a proposed revision waits for you" in out, "resume does not apply what is not approved"
    code, out, err = run(capsys, "approve", "--output", ws, "--revision")
    assert code == 0 and "approved revision plans/revisions/revise_r02.json" in out
    calls = len(fake.calls)
    code, out, err = run(capsys, "resume", "--output", ws, "--filmcraft", CLI)
    assert code == 0, out + err
    assert len(fake.calls) == calls, "the approved proposal is applied without asking the model again"
    plan3 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert [c.clip_instance_id for c in plan3.scenes[0].clips] == ids2[:2] and plan3.plan_hash == prop["plan_hash"]
    man = rj(ws / "manifest.json")
    assert man["approval"]["mode"] == "revision" and man["approval"]["authorization"] == "manual" and man["approval"]["parent_plan_hash"] == plan2.plan_hash
    assert man["filmcraft"]["project_version"] == 3 and len(list((ws / "filmcraft").glob("*.fcproj"))) == 3
    # 11 s -> 8 s is outside the tolerance of the 12 s target: the target moved with the revision, not in project.yaml
    assert plan3.project.target_duration_seconds == pytest.approx(plan3.total_duration_seconds) and man["revise"]["overrides"]["target_duration_seconds"] == pytest.approx(8.0)
    assert (ws / "project.yaml").read_bytes() == yaml_before and rj(ws / "plans" / "revisions" / "revise_r02.json")["status"] == "applied"
    qc = rj(ws / "preview" / "qc_report.json")
    assert qc["plan_hash"] == plan3.plan_hash and {c["name"]: c["status"] for c in qc["checks"]}["total_duration"] == "pass"
    assert fc_sequence(ws, media, ws / man["filmcraft"]["project"]).duration_frames == plan3.total_duration_frames
    # everything after is cached: resume builds nothing and makes no request
    journal = (ws / "filmcraft" / "execution_journal.json").read_bytes()
    code, out, err = run(capsys, "resume", "--output", ws, "--filmcraft", CLI)
    assert code == 0 and (ws / "filmcraft" / "execution_journal.json").read_bytes() == journal and len(fake.calls) == calls
    code, out, _ = run(capsys, "status", "--output", ws)
    assert code == 0 and "revisions : 2 applied" in out and "revise_r02 applied" in out and "(revision of" in out and "_v003.fcproj" in out
    assert "revise_r01" in (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8") and "revise_r02" in (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")

    # ---- a revision on top of a revised plan, approved and applied with `revise --apply` -----------------------------------------
    ids3 = [c.clip_instance_id for c in plan3.scenes[0].clips]
    fake.respond = lambda call: answer([op("shorten_scene", scene_id=call.scenes[0], shorten_by_seconds=2.0)], ambiguous=[question("By how much?", "a bit shorter", "2 s")])
    code, out, err = run(capsys, "revise", "--output", ws, "make it a bit shorter", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 3, out + err
    code, out, err = run(capsys, "revise", "--output", ws, "--apply", "--filmcraft", CLI)
    assert code == 1 and "is not approved yet" in out + err
    assert run(capsys, "approve", "--output", ws, "--revision")[0] == 0
    code, out, err = run(capsys, "revise", "--output", ws, "--apply", "--filmcraft", CLI)
    assert code == 0, out + err
    plan4 = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert plan4.total_duration_seconds == pytest.approx(plan3.total_duration_seconds - 2.0, abs=0.05) and ids3 == [c.clip_instance_id for c in plan4.scenes[0].clips]
    assert rj(ws / "manifest.json")["filmcraft"]["project_version"] == 4 and hash_tree(media) == before
