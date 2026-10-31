"""Unit tests for the re-seal helpers (the end-to-end behaviour is in tests/integration/test_reseal_e2e.py)."""
from __future__ import annotations

from trip_vlog.reseal import backup_file


def test_backup_uses_the_stage_naming_and_never_overwrites_different_bytes(tmp_path):
    f = tmp_path / "edit_plan.json"
    f.write_bytes(b"one")
    assert backup_file(f, "a" * 64) == f"edit_plan.{'a' * 12}.bak.json"
    assert backup_file(f, "a" * 64) == f"edit_plan.{'a' * 12}.bak.json"       # identical bytes: the same backup, no duplicate
    f.write_bytes(b"two")
    second = backup_file(f, "a" * 64)
    assert second == f"edit_plan.{'a' * 12}.bak2.json"
    assert (tmp_path / f"edit_plan.{'a' * 12}.bak.json").read_bytes() == b"one" and (tmp_path / second).read_bytes() == b"two"
    f.write_bytes(b"x")
    assert backup_file(f, "invalid-" + "b" * 40) == "edit_plan.invalidbbbb.bak.json"      # the "-" is dropped from invalid-... hashes, as the stages do


def test_reseal_persists_the_first_seal_when_the_second_raises(tmp_path, monkeypatch):
    import pytest

    from trip_vlog import reseal as R
    from trip_vlog.state import Manifest

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "project.yaml").write_text("x: 1\n")
    from trip_vlog.state import Workspace
    cp = Workspace(ws).catalog_path
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(b"")
    Manifest(ws / "manifest.json").save()
    sealed = R.SealResult.__new__(R.SealResult)
    # drive the loop through its seams: patch everything heavy
    class _Cat:
        def __init__(self, p): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(R, "Catalog", _Cat)
    monkeypatch.setattr(R, "load_config", lambda p: {})
    monkeypatch.setattr(R, "resolve_paths", lambda *a, **k: None)
    monkeypatch.setattr("trip_vlog.revision_common.apply_manifest_overrides", lambda c, m: None)
    monkeypatch.setattr("trip_vlog.outline_stages.outline_paths", lambda ctx: type("P", (), {"outline": ws / "o"})())
    (ws / "o").write_text("{}")
    monkeypatch.setattr("trip_vlog.draft_stages._pp", lambda ctx: type("P", (), {"edit_plan": ws / "p"})())
    (ws / "p").write_text("{}")
    sealed.__dict__.update(outcome="sealed", what="outline", previous_hash="a", new_hash="b", backup=None, repairs=[], approval=None, ok=True)
    monkeypatch.setattr(R, "_seal_outline", lambda ctx, root: sealed)
    def boom(ctx, root): raise OSError("disk")
    monkeypatch.setattr(R, "_seal_plan", boom)
    with pytest.raises(OSError):
        R.reseal(ws)
    assert Manifest.load(ws / "manifest.json").get("reseal")[0]["what"] == "outline"
