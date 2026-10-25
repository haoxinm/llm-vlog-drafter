from __future__ import annotations

from pathlib import Path

import pytest

from trip_vlog.config import Config, ConfigError, load_config, parse_config, resolve_paths

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "project.yaml"


def test_defaults():
    c = Config()
    assert c.media.vision_sampling_fps == 1.0
    assert c.media.fingerprint == "fast"
    assert c.workflow.approval == "stop_after_outline"
    assert c.filmcraft.control_host == "127.0.0.1"


def test_example_config_loads_and_matches_defaults():
    assert load_config(EXAMPLE).to_dict() == Config().to_dict()


def test_frame_rate_accepts_numbers_and_rationals():
    assert parse_config({"project": {"frame_rate": 30}}).project.frame_rate == "30"
    assert parse_config({"project": {"frame_rate": "30000/1001"}}).project.frame_rate == "30000/1001"
    with pytest.raises(ConfigError):
        parse_config({"project": {"frame_rate": "0"}})
    with pytest.raises(ConfigError):
        parse_config({"project": {"frame_rate": "fast"}})


@pytest.mark.parametrize("raw", [
    {"media": {"fingerprint": "md5"}},
    {"media": {"vision_sampling_fps": 0}},
    {"media": {"vision_sampling_fps": 999}},
    {"workflow": {"approval": "yolo"}},
    {"project": {"aspect_ratio": "wide"}},
    {"project": {"aspect_ratio": "0:9"}},
    {"project": {"target_duration_seconds": -5}},
    {"project": {"music_policy": "stream_anything"}},
    {"editorial": {"max_similar_shots_per_scene": 0}},
    {"filmcraft": {"mode": "telepathy"}},
    {"filmcraft": {"control_host": "0.0.0.0"}},
    {"filmcraft": {"control_port": 70000}},
    {"media": {"unknown_field": 1}},
    {"nonsense": {}},
    {"media": {"local_only": True}},  # the default llm endpoint is a remote host
    {"media": {"fine_sampling_enabled": True, "fine_sampling_fps": 0.5}},
])
def test_invalid_values_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_local_only_with_loopback_endpoint_ok():
    ep = {"api": "openai_chat", "base_url": "http://127.0.0.1:8080", "api_key_env": None}
    c = parse_config({"media": {"local_only": True}, "llm": {"endpoints": {"main": ep}}})
    assert c.media.local_only


def test_overrides_win_and_none_ignored():
    c = parse_config({"media": {"fingerprint": "fast"}}, {"media.fingerprint": "sha256", "project.title": None})
    assert c.media.fingerprint == "sha256" and c.project.title == "Trip Vlog"
    with pytest.raises(ConfigError):
        parse_config({}, {"media.fingerprint": "nope"})


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_config_hash_changes_with_config():
    assert Config().config_hash() == Config().config_hash()
    assert Config().config_hash() != parse_config({"project": {"title": "X"}}).config_hash()


@pytest.mark.parametrize("bad", ["/etc/out.json", "../escape.json", "a/../../b", "C:/abs/path", "C:rel", "//server/share/x",
                                 "con/x.json", "bad<name>.json", "trailing./x", ""])
def test_outputs_must_be_relative_and_portable(bad):
    with pytest.raises(ConfigError):
        parse_config({"outputs": {"preview_file": bad}})


def test_outputs_backslashes_normalised():
    assert parse_config({"outputs": {"preview_file": "preview\\draft.mp4"}}).outputs.preview_file == "preview/draft.mp4"


def test_resolve_paths_ok(tmp_path):
    media, ws = tmp_path / "media", tmp_path / "ws"
    media.mkdir()
    rp = resolve_paths(Config(), media, ws)
    assert rp.workspace == ws.resolve() and rp.media_root == media.resolve()
    assert rp.outputs["preview_file"] == (ws / "preview" / "draft.mp4").resolve()


def test_media_root_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_paths(Config(), tmp_path / "nope", tmp_path / "ws")
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        resolve_paths(Config(), f, tmp_path / "ws")


def test_workspace_inside_media_rejected(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    with pytest.raises(ConfigError, match="inside the media folder"):
        resolve_paths(Config(), media, media / "out")
    with pytest.raises(ConfigError):
        resolve_paths(Config(), media, media)


def test_media_inside_workspace_rejected(tmp_path):
    ws = tmp_path / "ws"
    (ws / "media").mkdir(parents=True)
    with pytest.raises(ConfigError, match="inside the output workspace"):
        resolve_paths(Config(), ws / "media", ws)


def test_workspace_must_not_be_a_file(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    f = tmp_path / "ws"
    f.write_text("x")
    with pytest.raises(ConfigError):
        resolve_paths(Config(), media, f)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_output_symlink_escape_rejected(tmp_path):
    media, ws, elsewhere = tmp_path / "media", tmp_path / "ws", tmp_path / "elsewhere"
    media.mkdir(); ws.mkdir(); elsewhere.mkdir()
    try:
        (ws / "preview").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ConfigError, match="outside the workspace"):
        resolve_paths(Config(), media, ws)
