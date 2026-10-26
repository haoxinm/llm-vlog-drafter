"""`trip-vlog reseal` end to end on the ffmpeg-generated fixtures: a hand-edited plan or outline is re-validated and sealed explicitly; nothing is approved.
The gate tests need ffmpeg only; the revision-round test also needs $FILMCRAFT_CLI."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport
from analysis_helpers import gateway_for as vision_gateway
from conftest import hash_tree
from outline_helpers import OutlineTransport
from outline_helpers import gateway_for as text_gateway
from revise_helpers import ReviseTransport, answer, op
from revise_helpers import gateway_for as revise_gateway

from trip_vlog import cli
from trip_vlog import outline_stages as os_
from trip_vlog import revise_stages as rs
from trip_vlog.analysis import stage as st
from trip_vlog.planning.models import EditPlan, edit_plan_hash

CLI = os.environ.get("FILMCRAFT_CLI")
needs_fc = pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")
pytestmark = pytest.mark.integration
ENDPOINT = "llm.endpoints={main: {api: anthropic_messages, model: test-model}}"


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def drafted(media_dir, workspace, capsys):
    code, out, err = run(capsys, "draft", "--media", media_dir, "--output", workspace, "--duration", 12)
    assert code == 3, out + err
    return workspace


def edit_rationale(ws: Path, text: str = "Kept because I like it", bom: bool = False) -> dict:
    p = ws / "plans" / "edit_plan.json"
    doc = rj(p)
    doc["scenes"][0]["clips"][0]["rationale"] = text
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(doc, indent=2).encode("utf-8"))
    return doc


def test_a_hand_edited_plan_is_refused_until_resealed_and_then_needs_its_own_approval(drafted, media_dir, capsys):
    ws, before = drafted, hash_tree(media_dir)
    assert run(capsys, "approve", "--output", ws)[0] == 0
    approved = rj(ws / "manifest.json")["approval"]["plan_hash"]
    old_text = (ws / "plans" / "edit_plan.json").read_text(encoding="utf-8")
    edit_rationale(ws, bom=True)                                            # an editor on Windows adds a byte order mark
    code, _, err = run(capsys, "approve", "--output", ws)
    assert code == 4 and "plan_hash_mismatch" in err                        # today's behaviour: the seal does not match

    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 0, out + err
    assert "edit plan plans/edit_plan.json: sealed" in out and "invalidated" in out and "nothing was approved" in out
    plan = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert plan.plan_hash == edit_plan_hash(plan) != approved and plan.scenes[0].clips[0].rationale == "Kept because I like it"
    assert not (ws / "plans" / "edit_plan.json").read_bytes().startswith(b"\xef\xbb\xbf")
    baks = list((ws / "plans").glob("edit_plan.*.bak.json"))
    assert len(baks) == 1 and baks[0].read_bytes().startswith(b"\xef\xbb\xbf") and "Kept because I like it" in baks[0].read_text(encoding="utf-8-sig")
    assert old_text not in baks[0].read_text(encoding="utf-8-sig")
    m = rj(ws / "manifest.json")
    assert m["approval"]["status"] == "invalidated" and m["approval"]["previous_plan_hash"] == approved       # never approved by reseal
    assert m["reseal"][0]["what"] == "plan" and m["reseal"][0]["hash"] == plan.plan_hash and m["reseal"][0]["backup"] == baks[0].name
    assert m["plan"]["plan_hash"] == approved, "the recorded hash of the generated plan is untouched, so a later re-plan still backs the edit up"
    assert "re-sealed" in (ws / "plans" / "plan_summary.md").read_text(encoding="utf-8") and rj(ws / "plans" / "reseal_report.json")["ok"] is True

    code, out, _ = run(capsys, "reseal", "--output", ws)                  # idempotent: nothing to do the second time, no new backup
    assert code == 0 and "unchanged" in out and "already sealed" in out and len(list((ws / "plans").glob("edit_plan.*.bak.json"))) == 1
    assert run(capsys, "approve", "--output", ws)[0] == 0                 # the explicit approval is a separate step
    assert rj(ws / "manifest.json")["approval"]["plan_hash"] == plan.plan_hash
    assert hash_tree(media_dir) == before


def test_an_invalid_plan_is_not_sealed_and_nothing_changes(drafted, capsys):
    ws = drafted
    p = ws / "plans" / "edit_plan.json"
    doc = rj(p)
    doc["scenes"][0]["clips"][0]["source_out_seconds"] = 99.0
    doc["scenes"][0]["clips"][1]["asset_id"] = "asset_0123456789abcdef"
    p.write_text(json.dumps(doc), encoding="utf-8")
    bytes_before, man_before = p.read_bytes(), (ws / "manifest.json").read_bytes()
    code, out, err = run(capsys, "reseal", "--plan", "--output", ws)
    assert code == 4 and "NOT sealed" in err and "asset_not_found" in err and "99" in err
    assert p.read_bytes() == bytes_before and (ws / "manifest.json").read_bytes() == man_before
    assert not list((ws / "plans").glob("*.bak.json")) and rj(ws / "plans" / "reseal_report.json")["ok"] is False
    p.write_text("{ not json", encoding="utf-8")
    code, _, err = run(capsys, "reseal", "--output", ws)
    assert code == 4 and "not readable JSON" in err


def test_the_duration_tolerance_and_must_exclude_are_enforced_by_reseal(drafted, capsys):
    ws = drafted
    p = ws / "plans" / "edit_plan.json"
    doc = rj(p)
    doc["project"]["target_duration_seconds"] = 60.0                       # the plan is now far from its own target; the config target still decides
    p.write_text(json.dumps(doc), encoding="utf-8")
    import yaml
    cfg = ws / "project.yaml"
    y = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    y.setdefault("project", {})["target_duration_seconds"] = 60
    cfg.write_text(yaml.safe_dump(y), encoding="utf-8")
    code, out, err = run(capsys, "reseal", "--plan", "--output", ws)
    assert code == 4 and "NOT sealed" in err and "duration" in err.lower()


def test_reseal_needs_a_workspace_and_something_to_seal(tmp_path, capsys):
    code, out, err = run(capsys, "reseal", "--output", tmp_path / "nope")
    assert code == 2 and "not a trip-vlog workspace" in err


def test_a_half_applied_round_blocks_reseal(drafted, capsys):
    ws = drafted
    m = rj(ws / "manifest.json")
    m["revise"] = {"applying": {"tag": "revise_r01"}}
    (ws / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 2 and "interrupted" in err and "resume" in err


# ---- the story outline ---------------------------------------------------------------------------------------------

@pytest.fixture
def outlined(media_dir, workspace, capsys):
    vision, text = LabelingTransport(best=lambda sid, n: None), OutlineTransport()
    st.gateway_factory = lambda c: vision_gateway(c.config, c.workspace.root, vision)
    os_.gateway_factory = lambda c: text_gateway(c.config, c.workspace.root, text)
    try:
        code, out, err = run(capsys, "outline", "--media", media_dir, "--output", workspace, "--set", ENDPOINT, "--duration", 20)
        assert code == 3, out + err
        assert run(capsys, "approve", "--output", workspace)[0] == 0
        yield workspace, text
    finally:
        st.gateway_factory = None
        os_.gateway_factory = None


def test_a_hand_edited_outline_is_validated_backed_up_and_its_approval_withdrawn(outlined, capsys):
    ws, text = outlined
    p = ws / "plans" / "story_outline.json"
    doc = rj(p)
    approved = rj(ws / "manifest.json")["outline_approval"]["outline_hash"]
    doc["scenes"][0]["title"] = "Edited by hand"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))        # compact, with a BOM
    code, out, err = run(capsys, "reseal", "--outline", "--output", ws)
    assert code == 0, out + err
    assert "story outline plans/story_outline.json: sealed" in out and "backup" in out and "invalidated" in out
    assert rj(p)["scenes"][0]["title"] == "Edited by hand" and not p.read_bytes().startswith(b"\xef\xbb\xbf") and p.read_text(encoding="utf-8").startswith("{\n")
    assert len(list((ws / "plans").glob("story_outline.*.bak.json"))) == 1
    assert "Edited by hand" in (ws / "plans" / "story_outline.md").read_text(encoding="utf-8")
    m = rj(ws / "manifest.json")
    assert m["outline_approval"]["status"] == "invalidated" and m["outline_approval"]["previous_outline_hash"] == approved and m["reseal"][0]["what"] == "outline"
    assert len(text.calls) == 1, "no model was called"
    code, out, _ = run(capsys, "reseal", "--outline", "--output", ws)
    assert code == 0 and "unchanged" in out
    assert run(capsys, "approve", "--outline", "--output", ws)[0] == 0 and rj(ws / "manifest.json")["outline_approval"]["status"] == "approved"


def test_an_invalid_outline_is_not_sealed(outlined, capsys):
    ws, _ = outlined
    p = ws / "plans" / "story_outline.json"
    doc = rj(p)
    doc["scenes"][0]["candidate_moment_ids"] = ["moment_ffffffffffffffff"]
    p.write_text(json.dumps(doc), encoding="utf-8")
    before = p.read_bytes()
    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 4 and "unknown_moment" in err and p.read_bytes() == before and not list((ws / "plans").glob("*.bak.json"))
    assert rj(ws / "manifest.json")["outline_approval"]["status"] == "approved", "a refused seal leaves the approval alone"


# ---- a plan produced by a revision round ---------------------------------------------------------------------------

@needs_fc
def test_a_plan_from_a_revision_round_can_be_edited_and_resealed(media_dir, tmp_path, capsys, monkeypatch):
    t = ReviseTransport()
    monkeypatch.setattr(rs, "gateway_factory", lambda c: revise_gateway(c.config, c.workspace.root, t))
    ws = tmp_path / "ws"
    code, out, err = run(capsys, "draft", "--media", media_dir, "--output", ws, "--duration", 12, "--auto-approve", "--filmcraft", CLI, "--set", ENDPOINT)
    assert code == 0, out + err
    t.respond = lambda call: answer([op("set_clip_audio", clip_instance_id=call.clips[0], audio="mute", part="mute the first clip")], summary="Mute the first clip.")
    code, out, err = run(capsys, "revise", "--output", ws, "mute the first clip", "--set", ENDPOINT, "--filmcraft", CLI)
    assert code == 0, out + err
    revised = rj(ws / "plans" / "edit_plan.json")
    assert rj(ws / "manifest.json")["approval"]["mode"] == "revision"
    edit_rationale(ws, "Hand-written note after the revision")
    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 0, out + err
    new = EditPlan.model_validate(rj(ws / "plans" / "edit_plan.json"))
    assert new.plan_hash != revised["plan_hash"] and new.scenes[0].clips[0].audio_policy == "muted"
    assert rj(ws / "manifest.json")["approval"]["status"] == "invalidated"
    assert run(capsys, "approve", "--output", ws)[0] == 0
    code, out, err = run(capsys, "resume", "--output", ws, "--filmcraft", CLI)
    assert code == 0, out + err
    assert rj(ws / "manifest.json")["filmcraft"]["project_version"] == 2, "a note does not change the timeline, so the build is a no-op; the handoff is still refreshed"
    assert "re-sealed with `trip-vlog reseal`" in (ws / "resolve_handoff" / "handoff_report.md").read_text(encoding="utf-8")


def test_reseal_reports_the_approval_accurately_and_warns_when_critic_and_revise_will_refuse(drafted, capsys):
    ws = drafted
    edit_rationale(ws)                                                      # a note: no approval recorded, the timeline is unchanged
    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 0 and rj(ws / "manifest.json")["reseal"][-1]["approval"] == "none" and "critic/revise" not in out
    assert "approval: none recorded yet" in out and "approval: kept" not in out

    assert run(capsys, "approve", "--output", ws)[0] == 0
    code, out, _ = run(capsys, "reseal", "--output", ws)                    # sealed and approved: unchanged -> the approval is the plan's own
    assert "unchanged" in out and "still applies" in out

    m = rj(ws / "manifest.json")                                            # an approval of some other plan is not "kept" and is not claimed as invalidated by this seal
    m["approval"]["plan_hash"] = "f" * 64
    (ws / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    edit_rationale(ws, "again")
    code, out, _ = run(capsys, "reseal", "--output", ws)
    assert code == 0 and rj(ws / "manifest.json")["reseal"][-1]["approval"] == "for another plan" and "for another plan" in out
    assert rj(ws / "manifest.json")["approval"]["plan_hash"] == "f" * 64

    doc = rj(ws / "plans" / "edit_plan.json")                               # a change to the clips: critic and revise cannot take the plan as their base
    first = doc["scenes"][0]["clips"][0]
    first["audio_policy"] = "muted" if first["audio_policy"] != "muted" else "natural_sound"
    (ws / "plans" / "edit_plan.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    code, out, err = run(capsys, "reseal", "--output", ws)
    assert code == 0, out + err
    assert "critic/revise: they will refuse this plan until it is re-planned" in out and "`trip-vlog critic` and `trip-vlog revise` will refuse" in out
