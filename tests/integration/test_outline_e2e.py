"""`trip-vlog outline` end to end on the ffmpeg-generated fixtures with fake HTTP transports (skipped without ffmpeg)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport
from analysis_helpers import gateway_for as vision_gateway
from conftest import hash_tree
from outline_helpers import OutlineTransport
from outline_helpers import gateway_for as text_gateway

from trip_vlog import cli
from trip_vlog import outline_stages as os_
from trip_vlog.analysis import stage as st

pytestmark = pytest.mark.integration

ENDPOINT = "llm.endpoints={main: {api: anthropic_messages, model: test-model}}"


@pytest.fixture
def fakes():
    vision = LabelingTransport(best=lambda sid, n: None)
    text = OutlineTransport()
    st.gateway_factory = lambda c: vision_gateway(c.config, c.workspace.root, vision)
    os_.gateway_factory = lambda c: text_gateway(c.config, c.workspace.root, text)
    yield vision, text
    st.gateway_factory = None
    os_.gateway_factory = None


def rj(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_outline_to_approval_and_back(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    before = hash_tree(media_dir)
    code, out, _ = run(capsys, "outline", "--media", media_dir, "--output", workspace, "--set", ENDPOINT, "--duration", 20)
    assert code == 3 and "AWAITING APPROVAL" in out and "story_outline.md" in out and f"trip-vlog approve --output {workspace}" in out
    man = rj(workspace / "manifest.json")
    assert man["pipeline"] == "outline" and man["stages"]["analyze"]["status"] == "succeeded" and man["stages"]["outline"]["status"] == "succeeded"
    assert man["stages"]["outline_approval"]["status"] == "awaiting_approval" and man["outline_approval"]["status"] == "pending"
    assert len(text.calls) == 1 and text.calls[0].target == 20 and len(vision.calls) >= 2
    plans = workspace / "plans"
    doc = rj(plans / "story_outline.json")
    assert doc["schema_version"] == "1" and sum(s["target_duration_seconds"] for s in doc["scenes"]) == pytest.approx(20, abs=1.0)
    ids = {m for s in doc["scenes"] for m in s["candidate_moment_ids"]}
    moments = {json.loads(l)["moment_id"]: json.loads(l) for l in (workspace / "media_index" / "moments.jsonl").read_text(encoding="utf-8").splitlines()}
    assert ids and all(i in moments and moments[i]["media_type"] in ("video", "image") for i in ids)          # audio-only assets never offered
    prompt = text.calls[0].texts[0]
    assert "<untrusted_media_data" in prompt and "gps" not in prompt.lower() and "A test scene" in prompt     # labels reached the story editor
    assert not (workspace / "filmcraft").exists() and not (plans / "edit_plan.json").exists()                  # nothing built, nothing selected
    assert (plans / "story_outline.md").is_file() and (plans / "story_outline.schema.json").is_file()
    assert hash_tree(media_dir) == before

    code, out, _ = run(capsys, "status", "--output", workspace)
    assert code == 0 and "outline" in out and "awaiting_approval" in out and "next: review plans/story_outline.md" in out

    code, out, _ = run(capsys, "approve", "--output", workspace)
    assert code == 0 and "approved story outline" in out
    code, out, _ = run(capsys, "resume", "--output", workspace)
    assert code == 0 and "outline approved (manual)" in out and len(text.calls) == 1                           # nothing recomputed
    man = rj(workspace / "manifest.json")
    assert man["outline_approval"]["mode"] == "manual" and man["stages"]["outline"]["decision"] == "cache_hit"
    code, out, _ = run(capsys, "status", "--output", workspace)
    assert "outline   : approved (manual)" in out

    # the user edits the outline: approval is withdrawn, no LLM call, the summary follows
    doc["scenes"][0]["title"] = "Edited by hand"
    (plans / "story_outline.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    code, out, _ = run(capsys, "resume", "--output", workspace)
    assert code == 3 and "outline changed since approval" in out and len(text.calls) == 1
    assert "Edited by hand" in (plans / "story_outline.md").read_text(encoding="utf-8")
    code, out, err = run(capsys, "approve", "--outline", "--output", workspace)
    assert code == 0 and "approved story outline" in out

    # a broken edit is refused by approve (exit 4) and fails resume
    doc["scenes"][0]["candidate_moment_ids"] = ["moment_ffffffffffffffff"]
    (plans / "story_outline.json").write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = run(capsys, "approve", "--output", workspace)
    assert code == 4 and "invalid story outline" in err and "unknown_moment" in err
    code, out, _ = run(capsys, "resume", "--output", workspace)
    assert code == 1 and "story outline is not valid" in out


def test_changing_the_duration_regenerates_and_keeps_the_edit(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    run(capsys, "outline", "--media", media_dir, "--output", workspace, "--set", ENDPOINT, "--duration", 20)
    p = workspace / "plans" / "story_outline.json"
    doc = rj(p)
    doc["premise"] = "MY PREMISE"
    p.write_text(json.dumps(doc), encoding="utf-8")
    code, out, _ = run(capsys, "outline", "--output", workspace, "--duration", 30)                      # --media comes from the manifest
    assert code == 3 and len(text.calls) == 2 and text.calls[1].target == 30 and "a copy is at" in out
    assert rj(p)["premise"] != "MY PREMISE" and any(f.name.endswith(".bak.json") for f in (workspace / "plans").iterdir())


def test_dry_run_sends_nothing_and_reports(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    code, out, _ = run(capsys, "outline", "--media", media_dir, "--output", workspace, "--set", ENDPOINT, "--dry-run")
    assert code == 0 and "outline dry run (nothing is sent)" in out and "endpoint 'main' (remote)" in out and "model test-model" in out
    assert "moments in the prompt" in out and "estimated input" in out and "trip-vlog analyze" in out        # unlabeled: analyze has not run
    assert vision.calls == [] and text.calls == [] and not (workspace / "plans" / "story_outline.json").exists()


def test_auto_approve_runs_straight_through(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    code, out, _ = run(capsys, "outline", "--media", media_dir, "--output", workspace, "--set", ENDPOINT, "--auto-approve")
    assert code == 0 and "outline approved (automatic)" in out and "AWAITING" not in out
    assert rj(workspace / "manifest.json")["outline_approval"]["mode"] == "automatic"
    code, out, _ = run(capsys, "approve", "--output", workspace)                                          # still allowed, records manual
    assert code == 0


def test_baseline_draft_approval_is_unchanged(media_dir, workspace, capsys):
    code, out, _ = run(capsys, "draft", "--media", media_dir, "--output", workspace, "--duration", 10, "--filmcraft", "definitely-not-installed")
    assert code == 3 and "AWAITING APPROVAL" in out
    code, out, _ = run(capsys, "approve", "--output", workspace)
    assert code == 0 and out.startswith("approved plan ")
    code, out, err = run(capsys, "approve", "--outline", "--output", workspace)
    assert code == 4 and "no successful story outline" in err
