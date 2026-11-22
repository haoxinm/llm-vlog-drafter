from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft import discovery
from trip_vlog.filmcraft.errors import CapabilityError, FilmCraftNotFound

FIX = Path(fcsupport.HERE)


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"#!/bin/sh\n")
    return p


def find(configured=None, env=None, plat="linux", which=lambda n: None, home=None):
    return discovery.find_filmcraft_cli(configured, env=env or {}, platform_name=plat, which=which, home=home)


def test_precedence_config_then_env_then_path_then_standard(tmp_path):
    cfg, env_exe, on_path = (_touch(tmp_path / n / "filmcraft-cli") for n in ("cfg", "env", "path"))
    std = _touch(tmp_path / "home" / ".local" / "bin" / "filmcraft-cli")
    env = {"FILMCRAFT_CLI": str(env_exe)}
    which = lambda n: str(on_path)
    assert find(str(cfg), env, which=which, home=tmp_path / "home") == discovery.FoundExecutable(cfg.resolve(), "config")
    assert find(None, env, which=which, home=tmp_path / "home").source == "env"
    assert find(None, {}, which=which, home=tmp_path / "home").source == "PATH"
    found = find(None, {}, home=tmp_path / "home")
    assert (found.path, found.source) == (std.resolve(), "standard-location")


def test_directory_values_resolve_to_the_executable_inside(tmp_path):
    exe = _touch(tmp_path / "bin" / "filmcraft-cli")
    assert find(str(tmp_path / "bin")).path == exe.resolve()


def test_windows_uses_exe_suffix_and_program_files_locations(tmp_path):
    pf, local = tmp_path / "Program Files", tmp_path / "Local"
    exe = _touch(pf / "FilmCraft" / "filmcraft-cli.exe")
    env = {"ProgramFiles": str(pf), "LOCALAPPDATA": str(local)}
    assert discovery.exe_name("win32") == "filmcraft-cli.exe"
    locs = discovery.standard_locations("win32", env, tmp_path / "home")
    assert exe in locs and local / "FilmCraft" / "filmcraft-cli.exe" in locs
    assert find(None, env, plat="win32", home=tmp_path / "home").path == exe.resolve()
    # LOCALAPPDATA install
    exe2 = _touch(local / "Programs" / "FilmCraft" / "filmcraft-cli.exe")
    exe.unlink()
    assert find(None, {"LOCALAPPDATA": str(local)}, plat="win32", home=tmp_path / "home").path == exe2.resolve()


def test_macos_locations_include_homebrew_and_user_bin(tmp_path):
    locs = [str(p) for p in discovery.standard_locations("darwin", {}, tmp_path)]
    assert str(Path("/opt/homebrew/bin/filmcraft-cli")) in locs and str(Path("/usr/local/bin/filmcraft-cli")) in locs
    assert any(l.endswith(str(Path(".local") / "bin" / "filmcraft-cli")) for l in locs)
    assert any("FilmCraft.app" in l for l in locs)
    exe = _touch(tmp_path / ".local" / "bin" / "filmcraft-cli")
    assert find(None, {}, plat="darwin", home=tmp_path).path == exe.resolve()


def test_explicit_but_missing_path_is_an_error_not_a_fallback(tmp_path):
    other = _touch(tmp_path / "other" / "filmcraft-cli")
    with pytest.raises(FilmCraftNotFound, match="FILMCRAFT_CLI"):
        find(None, {"FILMCRAFT_CLI": str(tmp_path / "missing")}, which=lambda n: str(other))
    with pytest.raises(FilmCraftNotFound, match="filmcraft.executable"):
        find(str(tmp_path / "missing"), {}, which=lambda n: str(other))


def test_not_found_message_is_actionable(tmp_path):
    with pytest.raises(FilmCraftNotFound) as ei:
        find(None, {}, home=tmp_path)
    msg = str(ei.value)
    assert "FILMCRAFT_CLI" in msg and "filmcraft.executable" in msg and "PATH" in msg and "cargo build" in msg


def test_read_version_with_fake_and_garbage(tmp_path):
    ver, raw = discovery.read_version(fcsupport.FAKE_ARGV)
    assert ver == "0.13.0-fake" and raw.startswith("filmcraft-cli")
    junk = tmp_path / "junk.py"
    junk.write_text("print('hello')")
    with pytest.raises(FilmCraftNotFound, match="does not look like filmcraft-cli"):
        discovery.read_version([sys.executable, str(junk)])
    with pytest.raises(FilmCraftNotFound, match="cannot run"):
        discovery.read_version(str(tmp_path / "nope"))


def _fixture_inputs():
    tools = json.loads((FIX / "mcp_tools.json").read_text(encoding="utf-8"))
    commands = json.loads((FIX / "command_catalog_subset.json").read_text(encoding="utf-8"))
    return tools, commands


def _build(tools, commands):
    return discovery.build_capability_map(executable="x", version="0.13.0", version_output="filmcraft-cli 0.13.0",
                                          server_info={"name": "filmcraft"}, protocol_version="2025-06-18",
                                          tools=tools, commands=commands)


def test_capability_map_from_recorded_0_13_0_responses_has_every_required_capability():
    tools, commands = _fixture_inputs()
    cmap = _build(tools, commands)
    assert cmap.missing_required() == []
    assert cmap.require() is cmap
    assert cmap.command_id("place_clip") == "timeline.place"
    assert cmap.capabilities["place_clip"].usable and "sourceIn" in cmap.capabilities["place_clip"].params
    assert cmap.capabilities["import_media"].kind == "tool" and cmap.capabilities["import_media"].schema
    d = cmap.to_dict()
    assert d["missing_required"] == [] and d["filmcraft"]["version"] == "0.13.0"
    json.dumps(d)  # serialisable


def test_missing_required_command_is_an_actionable_error():
    tools, commands = _fixture_inputs()
    commands = [c for c in commands if c["id"] != "graphics.newText"]
    with pytest.raises(CapabilityError) as ei:
        _build(tools, commands).require()
    assert ei.value.missing == ["add_title"]
    assert "graphics.newText" in str(ei.value) and "0.13.0" in str(ei.value) and "Install a FilmCraft build" in str(ei.value)


def test_missing_parameter_makes_a_capability_unusable():
    tools, commands = _fixture_inputs()
    commands = copy.deepcopy(commands)
    for c in commands:
        if c["id"] == "timeline.place":
            c["params"] = c["params"].replace('"sourceIn":ticks?,', "")
    with pytest.raises(CapabilityError, match="lacks parameter.*sourceIn"):
        _build(tools, commands).require()


def test_missing_required_tool_and_tool_argument():
    tools, commands = _fixture_inputs()
    with pytest.raises(CapabilityError) as ei:
        _build([t for t in tools if t["name"] != "media_import"], commands).require()
    assert "import_media" in ei.value.missing
    tools2 = copy.deepcopy(tools)
    for t in tools2:
        if t["name"] == "media_import":
            t["inputSchema"]["properties"].pop("paths")
    with pytest.raises(CapabilityError, match="paths"):
        _build(tools2, commands).require()


def test_optional_capabilities_may_be_absent():
    tools, commands = _fixture_inputs()
    cmap = _build([t for t in tools if t["name"] != "render_frame"], [c for c in commands if c["id"] != "edit.undo"])
    cmap.require()
    assert {c.name for c in cmap.missing_optional()} >= {"render_frame", "undo"}


def test_param_keys_grammar():
    assert discovery.param_keys('{"item":id|"items":[id],"track":"V1"|id|"A1" (sound only)?,"time":ticks}') == {"item", "items", "track", "time"}


def test_discover_with_fake_writes_snapshot(fc):
    adapter, media, ws = fc
    cmap = adapter.discover_capabilities()
    snap = json.loads((ws / "filmcraft" / "capability_snapshot.json").read_text(encoding="utf-8"))
    assert snap["filmcraft"]["version"] == "0.13.0-fake" == cmap.version
    assert snap["schema"] == discovery.SNAPSHOT_SCHEMA and snap["missing_required"] == []
    assert snap["capabilities"]["place_clip"]["id"] == "timeline.place"
    assert snap["platform"]["system"]
    assert adapter.discover_capabilities() is cmap  # cached


def test_discover_with_missing_command_fails_with_capability_error(fc, monkeypatch):
    adapter, media, ws = fc
    monkeypatch.setenv("FAKE_FC_OMIT_COMMAND", "clip.audioGain")
    with pytest.raises(CapabilityError, match="clip.audioGain"):
        adapter.discover_capabilities()
    # the snapshot is still written for debugging
    assert json.loads((ws / "filmcraft" / "capability_snapshot.json").read_text())["missing_required"] == ["audio_gain"]
