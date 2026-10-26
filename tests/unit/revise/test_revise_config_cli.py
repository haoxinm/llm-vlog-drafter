"""`revise.*` and `llm.revise` configuration, llm-check coverage, local_only, and the command line wiring (no network, no FilmCraft)."""
from __future__ import annotations

import json

import pytest
from revise_helpers import revise_config

from trip_vlog import cli
from trip_vlog.config import ConfigError, parse_config
from trip_vlog.llm.check import endpoints_to_check
from trip_vlog.llm.factory import make_gateway
from trip_vlog.state import Manifest, Workspace

EPS = {"main": {"api": "anthropic_messages", "model": "text-model"}, "edit": {"api": "anthropic_messages", "model": "edit-model"}}


def test_defaults_and_bounds_of_the_revise_section():
    r = parse_config({}).revise
    assert (r.auto_apply, r.max_ops, r.confirm_duration_change_pct, r.confirm_duration_change_min_seconds, r.confirm_removed_clips, r.confirm_scene_removal,
            r.max_duration_change_pct) == (True, 12, 15.0, 10.0, 3, True, 50.0)
    for bad in ({"max_ops": 0}, {"confirm_duration_change_pct": 101}, {"max_duration_change_pct": 100}, {"confirm_removed_clips": 0}, {"nope": 1}, {"max_request_chars": 5}):
        with pytest.raises(ConfigError):
            parse_config({"revise": bad})


def test_the_revise_profile_falls_back_to_the_text_profile_and_is_checked():
    cfg = parse_config({"llm": {"endpoints": EPS, "text": "main", "vision": "main"}})
    assert cfg.llm.endpoint_for("revise")[0] == "main"
    cfg = parse_config({"llm": {"endpoints": EPS, "text": "main", "vision": "main", "revise": "edit"}})
    assert cfg.llm.endpoint_for("revise")[0] == "edit" and cfg.llm.endpoint_for("text")[0] == "main"
    with pytest.raises(ConfigError, match="llm.revise refers to unknown endpoint profile 'ghost'"):
        parse_config({"llm": {"endpoints": EPS, "revise": "ghost"}})
    gw = make_gateway(cfg, "ws", "revise", mode="replay")
    assert gw.provider.endpoint == "edit"
    with pytest.raises(ValueError, match="purpose"):
        make_gateway(cfg, "ws", "nonsense")


def test_llm_check_covers_the_revise_profile_only_when_it_is_its_own():
    assert endpoints_to_check(parse_config({"llm": {"endpoints": EPS}}), None) == ["main"]
    cfg = parse_config({"llm": {"endpoints": EPS, "revise": "edit"}})
    assert endpoints_to_check(cfg, None) == ["main", "edit"]


def test_local_only_is_enforced_for_the_revise_profile():
    local = {"api": "openai_chat", "base_url": "http://127.0.0.1:8080", "model": "m"}
    remote = {"api": "anthropic_messages", "model": "r"}
    parse_config({"media": {"local_only": True}, "llm": {"endpoints": {"main": local}, "revise": "main"}})
    with pytest.raises(ConfigError, match="llm.revise uses endpoint 'edit'"):
        parse_config({"media": {"local_only": True}, "llm": {"endpoints": {"main": local, "edit": remote}, "revise": "edit"}})
    with pytest.raises(ConfigError, match="llm.text uses endpoint"):
        parse_config({"media": {"local_only": True}, "llm": {"endpoints": {"main": remote}}})      # the default revise profile is the text profile


def test_revision_overrides_are_runtime_only():
    cfg = revise_config()
    h, y = cfg.config_hash(), cfg.to_yaml()
    cfg.set_revision_overrides({"target_duration_seconds": 12.0, "must_include_waived": ["x"]})
    assert cfg.config_hash() == h and cfg.to_yaml() == y and "12" not in y.split("target_duration_seconds")[1][:6]
    assert cfg.effective_target_seconds == 12.0 and cfg.project.target_duration_seconds == 18.5
    cfg.set_revision_overrides(None)
    assert cfg.effective_target_seconds == 18.5 and cfg.revision_overrides == {}
    assert parse_config({"editorial": {"must_include": ["a", "b"]}}).effective_must_include == ["a", "b"]


# ---- command line ---------------------------------------------------------------------------------------------------------------

def test_cli_parses_revise_and_approve_revision(tmp_path):
    ap = cli.build_parser()
    a = ap.parse_args(["revise", "--output", str(tmp_path), "make it shorter", "--dry-run", "--set", "revise.max_ops=3", "--again"])
    assert a.command == "revise" and a.request == "make it shorter" and a.dry_run and a.again and not a.apply and cli._overrides(a) == {"revise.max_ops": 3}
    assert ap.parse_args(["revise", "--output", str(tmp_path), "--apply"]).apply
    assert ap.parse_args(["revise", "--output", str(tmp_path), "--file", "r.txt"]).file.name == "r.txt"
    assert ap.parse_args(["approve", "--output", str(tmp_path), "--revision"]).which == "revision"
    with pytest.raises(SystemExit):
        ap.parse_args(["approve", "--output", str(tmp_path), "--revision", "--critic"])


def test_cli_errors_exit_2_and_say_why(tmp_path, capsys):
    assert cli.main(["revise", "--output", str(tmp_path / "none"), "shorter"]) == 2
    assert "holds no draft" in capsys.readouterr().err
    ws = Workspace(tmp_path / "ws")
    ws.ensure_dirs()
    ws.project_yaml.write_text("{}\n", encoding="utf-8")
    m = Manifest(ws.manifest_path)
    m.set(pipeline="draft")
    m.save()
    for argv, why in ((["revise", "--output", str(ws.root)], "give a request"),
                      (["revise", "--output", str(ws.root), "x", "--apply"], "takes no new request"),
                      (["revise", "--output", str(ws.root), "--apply", "--dry-run"], "cannot be combined"),
                      (["revise", "--output", str(ws.root), "x", "--file", str(tmp_path / "r.txt")], "either as an argument or with --file"),
                      (["revise", "--output", str(ws.root), "--file", str(tmp_path / "missing.txt")], "cannot read the request file")):
        assert cli.main(argv) == 2, argv
        assert why in capsys.readouterr().err
    assert cli.main(["approve", "--output", str(ws.root), "--revision"]) == 4
    assert "no revision is waiting" in capsys.readouterr().err


def test_a_request_file_with_a_byte_order_mark_is_read(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake(output, config, overrides, **kw):
        seen.update(kw)
        raise ConfigError("stop here")

    monkeypatch.setattr(cli, "run_revise", fake)
    f = tmp_path / "r.txt"
    f.write_bytes("﻿make the beach shorter\r\nand quieter\r\n".encode("utf-8"))
    assert cli.main(["revise", "--output", str(tmp_path), "--file", str(f)]) == 2
    assert seen["request"].startswith("make the beach shorter") and "﻿" not in seen["request"]


def test_status_lists_the_revision_history(tmp_path, capsys):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_dirs()
    m = Manifest(ws.manifest_path)
    m.set(pipeline="draft", approval={"status": "approved", "mode": "revision", "plan_hash": "b" * 64, "parent_plan_hash": "a" * 64, "revision": "plans/revisions/revise_r01.json"},
          revise={"rounds": [{"status": "applied", "revision": "plans/revisions/revise_r01.json", "request": "make the beach scene shorter", "plan_hash": "b" * 64,
                              "project": "filmcraft/Trip Vlog Draft_v002.fcproj", "qc_before": "warn", "qc_after": "fail", "halt_note": "QC got worse after revise_r01: x"}],
                  "pending": {"status": "pending", "revision": "plans/revisions/revise_r02.json", "request": "drop the sunset", "plan_hash": "c" * 64}})
    m.stages["revise"] = {"status": "awaiting_approval"}
    m.save()
    assert cli.main(["status", "--output", str(ws.root)]) == 0
    out = capsys.readouterr().out
    assert "revisions : 1 applied, 1 waiting" in out and "revise_r01 applied" in out and "make the beach scene shorter" in out and "Trip Vlog Draft_v002.fcproj" in out
    assert "QC warn -> fail" in out and "WARNING: QC got worse" in out and "revise_r02 pending" in out and "(revision of aaaaaaaaaaaa, plans/revisions/revise_r01.json)" in out
    assert "approve --revision" in out
    j = json.loads((lambda: (cli.main(["status", "--output", str(ws.root), "--json"]), capsys.readouterr().out)[1])())
    assert j["manifest"]["revise"]["rounds"][0]["request"] == "make the beach scene shorter"
