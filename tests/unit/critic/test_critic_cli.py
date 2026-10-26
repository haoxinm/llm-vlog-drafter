"""Command line wiring of the critic (no network, no FilmCraft)."""
from __future__ import annotations

import pytest

from trip_vlog import cli


def test_cli_parses_critic_and_approve_critic(tmp_path, capsys):
    ap = cli.build_parser()
    a = ap.parse_args(["critic", "--output", str(tmp_path), "--mode", "apply", "--set", "qc.critic_max_rounds=2"])
    assert a.command == "critic" and a.mode == "apply" and cli._overrides(a) == {"qc.critic_max_rounds": 2}
    assert ap.parse_args(["approve", "--output", str(tmp_path), "--critic"]).which == "critic"
    with pytest.raises(SystemExit):
        ap.parse_args(["critic", "--output", str(tmp_path), "--mode", "off"])
    assert cli.main(["approve", "--output", str(tmp_path), "--critic"]) == 4
    assert "not a trip-vlog workspace" in capsys.readouterr().err
