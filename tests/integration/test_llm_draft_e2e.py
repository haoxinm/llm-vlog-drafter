"""`draft --planner llm` end to end on the ffmpeg-generated fixtures with fake HTTP transports (gate tests need ffmpeg only; the
FilmCraft tests also need $FILMCRAFT_CLI and are skipped without it)."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport
from analysis_helpers import gateway_for as vision_gateway
from conftest import hash_tree
from outline_helpers import gateway_for as text_gateway
from selection_helpers import PlannerTransport

from trip_vlog import cli
from trip_vlog import outline_stages as os_
from trip_vlog.analysis import stage as st
from trip_vlog.filmcraft.adapter import FilmCraftAdapter

CLI = os.environ.get("FILMCRAFT_CLI")
needs_fc = pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")
pytestmark = pytest.mark.integration
ENDPOINT = "llm.endpoints={main: {api: anthropic_messages, model: test-model}}"


@pytest.fixture
def fakes():
    vision = LabelingTransport(best=lambda sid, n: None)
    text = PlannerTransport()
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


def fc_titles(ws: Path, media: Path, project: Path):
    ad = FilmCraftAdapter(ws, media, executable=CLI, request_timeout=120)
    try:
        ad.open_project(project)
        seq = ad.inspect_sequence()
        v2 = seq.track("V2")
        return [(c.name, c.start_frame, c.end_frame) for c in (v2.clips if v2 else [])], seq
    finally:
        ad.close()


def test_llm_draft_stops_at_the_outline_then_resumes(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    before = hash_tree(media_dir)
    args = ("--media", media_dir, "--output", workspace, "--planner", "llm", "--set", ENDPOINT, "--duration", 12,
            "--filmcraft", "definitely-not-installed")
    code, out, _ = run(capsys, "draft", *args)
    assert code == 3 and "AWAITING APPROVAL of the story outline" in out and "story_outline.md" in out
    man = rj(workspace / "manifest.json")
    assert man["pipeline"] == "draft" and man["planner"] == "llm" and man["stages"]["outline_approval"]["status"] == "awaiting_approval"
    assert man["stages"]["plan"]["decision"] == "awaiting_approval" and len(text.plan_calls) == 0
    assert not (workspace / "plans" / "edit_plan.json").exists() and not (workspace / "filmcraft").exists()
    code, out, _ = run(capsys, "status", "--output", workspace)
    assert code == 0 and all(n in out for n in ("audio", "analyze", "outline_approval", "plan", "approval", "assemble", "handoff"))
    code, out, _ = run(capsys, "approve", "--output", workspace)
    assert code == 0 and "approved story outline" in out
    code, out, _ = run(capsys, "resume", "--output", workspace)               # FilmCraft is not installed: the plan is made, assemble fails
    man = rj(workspace / "manifest.json")
    assert man["stages"]["plan"]["status"] == "succeeded" and len(text.plan_calls) == 1 and len(text.outline_calls) == 1
    assert man["approval"]["mode"] == "derived_from_outline" and man["stages"]["assemble"]["status"] == "failed" and code == 1
    plan = rj(workspace / "plans" / "edit_plan.json")
    assert plan["schema_version"] == "1.1" and abs(plan["total_duration_seconds"] - 12) <= 2
    summary = (workspace / "plans" / "plan_summary.md").read_text(encoding="utf-8")
    assert "Planner: llm" in summary and "A test scene" in summary
    assert (workspace / "plans" / "selection_report.json").is_file() and (workspace / "plans" / "timeline_planner_response.schema.json").is_file()
    assert hash_tree(media_dir) == before


@needs_fc
def test_llm_draft_with_opening_title_through_filmcraft(media_dir, workspace, fakes, capsys):
    vision, text = fakes
    code, out, err = run(capsys, "draft", "--media", media_dir, "--output", workspace, "--planner", "llm", "--auto-approve", "--set", ENDPOINT,
                         "--duration", 12, "--filmcraft", CLI, "--set", "editorial.opening_title=true", "--set", "project.title=Test Trip")
    assert code == 0, out + err
    man = rj(workspace / "manifest.json")
    assert man["approval"]["mode"] == "automatic" and man["stages"]["handoff"]["status"] in ("succeeded", "partial")
    plan = rj(workspace / "plans" / "edit_plan.json")
    assert [(t["role"], t["text"], t["track"]) for t in plan["titles"]] == [("opening_title", "Test Trip", "V2")]
    proj = workspace / man["filmcraft"]["project"]
    titles, seq = fc_titles(workspace, media_dir, proj)
    t = plan["titles"][0]
    assert titles == [("Test Trip", t["start_frame"], t["start_frame"] + t["duration_frames"])]
    qc = rj(workspace / "preview" / "qc_report.json")
    checks = {c["name"]: c["status"] for c in qc["checks"]}
    assert checks["titles"] == "pass" and checks["project_reopens"] == "pass" and "fail" not in checks.values(), checks
    assert (workspace / "preview" / "draft.mp4").is_file()
    ho = workspace / "resolve_handoff"
    assert [x["text"] for x in rj(ho / "handoff_summary.json")["report"]["titles_not_carried"]] == ["Test Trip"] \
        if "report" in rj(ho / "handoff_summary.json") else True
    report = (ho / "handoff_report.md").read_text(encoding="utf-8")
    assert '"Test Trip" at frame 0' in report and "resolve-finish" in report
    assert rj(ho / "edit_plan.json")["titles"][0]["text"] == "Test Trip"
    code, out, _ = run(capsys, "resolve-finish", "--output", workspace, "--dry-run")
    assert "1 title(s)" in out or "title" in out


@needs_fc
def test_baseline_draft_with_opening_and_closing_title(media_dir, workspace, capsys):
    code, out, err = run(capsys, "draft", "--media", media_dir, "--output", workspace, "--auto-approve", "--duration", 12, "--filmcraft", CLI,
                         "--set", "editorial.opening_title=true", "--set", "editorial.closing_title=Thanks for watching", "--set", "project.title=Baseline Trip")
    assert code == 0, out + err
    man = rj(workspace / "manifest.json")
    plan = rj(workspace / "plans" / "edit_plan.json")
    assert [t["text"] for t in plan["titles"]] == ["Baseline Trip", "Thanks for watching"]
    titles, seq = fc_titles(workspace, media_dir, workspace / man["filmcraft"]["project"])
    assert [n for n, *_ in titles] == ["Baseline Trip", "Thanks for watching"]
    assert titles[1][2] == seq.duration_frames                                    # the closing title ends with the sequence
    assert {c["name"]: c["status"] for c in rj(workspace / "preview" / "qc_report.json")["checks"]}["titles"] == "pass"
    spec = rj(workspace / "filmcraft" / "timeline_spec.json") if (workspace / "filmcraft" / "timeline_spec.json").is_file() else None
    assert spec is None or len(spec["titles"]) == 2


def test_two_line_title_is_skipped_with_a_note(media_dir, workspace, capsys):
    """FilmCraft names a text clip after its first line, so a two-line title would fail the read-back: it is not created, and says why."""
    run(capsys, "draft", "--media", media_dir, "--output", workspace, "--auto-approve", "--duration", 12,
        "--filmcraft", "definitely-not-installed", "--set", 'editorial.closing_title="Thanks\\nfor watching"')
    plan = rj(workspace / "plans" / "edit_plan.json")
    assert plan["titles"] == [] and any("closing title was skipped" in u and "lines" in u for u in plan["uncertainties"])
