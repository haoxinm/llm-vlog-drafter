from __future__ import annotations

import os
import sys

import pytest

from trip_vlog.filmcraft.errors import ParameterError, PathPolicyError
from trip_vlog.filmcraft.safety import PathPolicy, validate_command, validate_tool_call


@pytest.fixture
def policy(tmp_path):
    media, ws = tmp_path / "media", tmp_path / "ws"
    media.mkdir()
    ws.mkdir()
    (media / "a.mp4").write_bytes(b"x")
    return PathPolicy(ws, media)


def test_reads_allowed_in_media_root_and_workspace(policy, tmp_path):
    assert policy.check_read(tmp_path / "media" / "a.mp4") == (tmp_path / "media" / "a.mp4").resolve()
    assert policy.check_read(tmp_path / "ws" / "filmcraft" / "x.fcproj")


def test_reads_outside_roots_refused(policy, tmp_path):
    other = tmp_path / "elsewhere" / "secret.mp4"
    with pytest.raises(PathPolicyError, match="outside the allowed read roots"):
        policy.check_read(other)
    with pytest.raises(PathPolicyError):
        policy.check_read(tmp_path / "media" / ".." / "elsewhere" / "x")


def test_writes_only_inside_workspace(policy, tmp_path):
    assert policy.check_write(tmp_path / "ws" / "preview" / "draft.mp4")
    with pytest.raises(PathPolicyError, match="only writes there"):
        policy.check_write(tmp_path / "media" / "a.mp4")  # originals are read-only
    with pytest.raises(PathPolicyError):
        policy.check_write(tmp_path / "ws" / ".." / "oops.mp4")
    with pytest.raises(PathPolicyError, match="workspace root itself"):
        policy.check_write(tmp_path / "ws")


@pytest.mark.parametrize("bad", ["", "   ", "relative/path.mp4", "./x.mp4", "~/x.mp4", "a\x00b", None, 5])
def test_non_absolute_or_malformed_paths_refused(policy, bad):
    with pytest.raises(PathPolicyError):
        policy.check_read(bad)
    with pytest.raises(PathPolicyError):
        policy.check_write(bad)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlink_escape_is_resolved_and_refused(policy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("secret")
    link = tmp_path / "ws" / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathPolicyError):
        policy.check_write(link / "x.txt")
    with pytest.raises(PathPolicyError):
        policy.check_read(link / "x.txt")


def test_allowlist_rejects_unknown_commands_and_params(policy, tmp_path):
    with pytest.raises(ParameterError, match="not on the adapter allowlist"):
        validate_command("file.delete", {}, policy)
    with pytest.raises(ParameterError, match="not on the adapter allowlist"):
        validate_command("app.quit", {}, policy)
    with pytest.raises(ParameterError, match="unknown parameter"):
        validate_command("file.newProject", {"name": "x", "shell": "rm -rf /"}, policy)
    with pytest.raises(ParameterError, match="missing required"):
        validate_command("timeline.place", {"item": 1}, policy)
    with pytest.raises(ParameterError, match="not on the adapter allowlist"):
        validate_tool_call("ui_click", {"id": "x"}, policy)
    with pytest.raises(ParameterError, match="unknown argument"):
        validate_tool_call("project_inspect", {"x": 1}, policy)


def test_place_parameter_validation(policy):
    ok = {"item": 3, "track": "V1", "frame": 10, "duration": 5}
    assert validate_command("timeline.place", ok, policy) == ok
    for bad, msg in [({"track": "video 1"}, "track must look like"), ({"frame": -1}, ">= 0"), ({"duration": 0}, "> 0"),
                     ({"item": "3"}, "must be an integer"), ({"audio": "yes"}, "boolean"), ({"audioTrack": "V2"}, "audioTrack"),
                     ({"seconds": 1.0}, "exactly one of")]:
        with pytest.raises(ParameterError, match=msg):
            validate_command("timeline.place", {**ok, **bad}, policy)


def test_output_commands_are_confined_to_the_workspace(policy, tmp_path):
    ws = tmp_path / "ws"
    p = validate_command("file.exportMedia", {"path": str(ws / "preview" / "draft.mp4"), "format": "h264", "wait": True}, policy)
    assert p["path"] == str((ws / "preview" / "draft.mp4").resolve())
    for cid, params in [("file.exportMedia", {"path": str(tmp_path / "draft.mp4")}),
                        ("file.saveAs", {"path": str(tmp_path / "media" / "x.fcproj")}),
                        ("file.exportInterchange", {"format": "otio", "path": str(tmp_path / "x.otio")}),
                        ("file.save", {"path": "relative.fcproj"})]:
        with pytest.raises(PathPolicyError):
            validate_command(cid, params, policy)
    with pytest.raises(ParameterError, match="format must be one of"):
        validate_command("file.exportInterchange", {"format": "../etc", "path": str(ws / "x")}, policy)
    with pytest.raises(ParameterError, match="format must be one of"):
        validate_command("file.exportMedia", {"path": str(ws / "x.mp4"), "format": "prores"}, policy)


def test_media_import_paths_checked(policy, tmp_path):
    good = str(tmp_path / "media" / "a.mp4")
    assert validate_tool_call("media_import", {"paths": [good]}, policy)["paths"] == [str((tmp_path / "media" / "a.mp4").resolve())]
    with pytest.raises(PathPolicyError):
        validate_tool_call("media_import", {"paths": [good, str(tmp_path / "other.mp4")]}, policy)
    with pytest.raises(ParameterError):
        validate_tool_call("media_import", {"paths": []}, policy)
    with pytest.raises(PathPolicyError):
        validate_tool_call("command_run", {"id": "file.open", "params": {"path": str(tmp_path / "nope.fcproj")}}, policy)


def test_gain_and_text_rules(policy):
    with pytest.raises(ParameterError, match="only mode 'set'"):
        validate_command("clip.audioGain", {"clips": [1], "db": -3, "mode": "normalizeAll"}, policy)
    with pytest.raises(ParameterError):
        validate_command("clip.audioGain", {"clips": [], "db": -3}, policy)
    with pytest.raises(ParameterError, match="0-based"):
        validate_command("graphics.newText", {"text": "hi", "track": -1}, policy)
    with pytest.raises(ParameterError):
        validate_command("graphics.newText", {"text": " "}, policy)
