"""resolve-finish against a workspace built by the real pipeline (needs ffmpeg and $FILMCRAFT_CLI) and a fake Resolve.

The XML is FilmCraft's own export, so this checks that the fake import, the expected timeline derived from the
timeline spec and the comparison agree on real handoff data (stills with digits in their names are misread and repaired).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "resolve"))
from fake_resolve import FakeResolve  # noqa: E402

from trip_vlog import cli
from trip_vlog.orchestrator import run_draft
from trip_vlog.resolve import finish

CLI = os.environ.get("FILMCRAFT_CLI")
needs_fc = pytest.mark.skipif(not CLI or not Path(CLI).is_file(), reason="$FILMCRAFT_CLI not set or not a file")
pytestmark = [pytest.mark.integration, needs_fc]


@pytest.fixture(scope="module")
def drafted(tmp_path_factory, generated_media):
    media = tmp_path_factory.mktemp("rf_e2e") / "media"
    shutil.copytree(generated_media, media)
    ws = tmp_path_factory.mktemp("rf_ws") / "ws"
    res = run_draft(media, ws, None, {"project.target_duration_seconds": 12, "workflow.approval": "auto_approve",
                                      "filmcraft.executable": CLI}, echo=lambda *_: None)
    assert res.exit_code == 0, res.stage_status
    return {"ws": ws, "media": media}


def test_finish_on_real_handoff_repairs_the_still_and_passes(drafted, monkeypatch, capsys):
    ws = drafted["ws"]
    fake = FakeResolve()
    monkeypatch.setattr(finish, "resolve_factory", lambda: (fake, {"diagnosis": []}))
    assert cli.main(["resolve-finish", "--output", str(ws)]) == 0
    rep = json.loads((ws / "resolve_handoff" / "resolve_finish_report.json").read_text(encoding="utf-8"))
    assert rep["status"] == "pass", [c for c in rep["final_checks"] if c["status"] == "fail"]
    reasons = [(r["reason"], r["result"], Path(r["expected_path"]).name) for r in rep["repairs"]]
    assert ("image sequence misread", "online", "still_1080p.png") in reasons
    checks = {c["check"]: c["status"] for c in rep["final_checks"]}
    assert checks["media_online"] == checks["clip_order"] == checks["record_inout"] == checks["markers"] == checks["duration"] == "pass"
    assert rep["project"]["name"].endswith("(trip-vlog v001)") and fake.pm.created == [rep["project"]["name"]]
    assert (ws / "resolve_handoff" / "resolve_finish_report.md").is_file()
    # the draft itself is untouched: the optional stage is recorded but is not part of `draft`
    m = json.loads((ws / "manifest.json").read_text(encoding="utf-8"))
    assert m["stages"]["resolve_finish"]["status"] == "succeeded" and m["pipeline"] == "draft"
    capsys.readouterr()
    assert cli.main(["status", "--output", str(ws)]) == 0
    assert "resolve   : succeeded" in capsys.readouterr().out


def test_dry_run_on_real_handoff(drafted, monkeypatch):
    fake = FakeResolve()
    monkeypatch.setattr(finish, "resolve_factory", lambda: (fake, {"diagnosis": []}))
    res = finish.run_finish(drafted["ws"], dry_run=True, echo=lambda *_: None)
    assert res.exit_code == 0 and not res.api.mutating_calls() and fake.pm.created == []
