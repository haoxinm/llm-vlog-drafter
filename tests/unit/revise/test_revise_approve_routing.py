"""An unflagged `approve` must not be captured by a stale `revise` awaiting_approval record."""
from __future__ import annotations

import pytest

from trip_vlog import orchestrator
from trip_vlog.orchestrator import revision_awaiting_approval
from trip_vlog.state import Manifest


def _manifest(tmp_path, revise_status, pending_status):
    m = Manifest(tmp_path / "manifest.json")
    m.stages["revise"] = {"status": revise_status}
    m.set(revise={"pending": {"status": pending_status}} if pending_status else {})
    return m


@pytest.mark.parametrize("stage,pend,expected", [
    ("awaiting_approval", "pending", True),
    ("awaiting_approval", "approved", False),
    ("awaiting_approval", "stale", False),
    ("awaiting_approval", None, False),
    ("succeeded", "pending", False),
])
def test_revision_awaiting_approval(tmp_path, stage, pend, expected):
    assert revision_awaiting_approval(_manifest(tmp_path, stage, pend)) is expected


def test_unflagged_approve_prefers_critic_over_stale_revise(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "awaiting_approval", "stale")
    m.stages["critic"] = {"status": "awaiting_approval"}
    m.save()
    (tmp_path / "project.yaml").write_text("x: 1\n")
    monkeypatch.setattr(orchestrator, "approve_critic", lambda out: {"ok": True})
    monkeypatch.setattr(orchestrator, "approve_revision", lambda out: pytest.fail("stale revise approved"))
    assert orchestrator.approve_pending(tmp_path) == ("critic", {"ok": True})


def test_unflagged_approve_routes_hash_stale_revision_to_refusal(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "awaiting_approval", "pending")
    m.save()
    called = []
    monkeypatch.setattr(orchestrator, "approve_revision", lambda out: called.append(out) or {"ok": True})
    monkeypatch.setattr(orchestrator, "approve_plan", lambda out: pytest.fail("edit plan approved instead of revision"))
    assert orchestrator.approve_pending(tmp_path) == ("revision", {"ok": True})
    assert called


def test_unflagged_approve_prefers_critic_when_revision_hash_stale(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "awaiting_approval", "pending")
    m.stages["critic"] = {"status": "awaiting_approval"}
    m.save()
    monkeypatch.setattr(orchestrator, "_revision_bound_to_current_plan", lambda manifest, ws: False)
    monkeypatch.setattr(orchestrator, "approve_critic", lambda out: {"ok": True})
    monkeypatch.setattr(orchestrator, "approve_revision", lambda out: pytest.fail("stale revise approved"))
    assert orchestrator.approve_pending(tmp_path) == ("critic", {"ok": True})
