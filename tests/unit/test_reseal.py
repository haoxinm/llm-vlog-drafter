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
