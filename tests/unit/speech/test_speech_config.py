from __future__ import annotations

import pytest

from trip_vlog.config import ConfigError, parse_config

LOCAL_LLM = {"endpoints": {"main": {"api": "openai_chat", "base_url": "http://127.0.0.1:11434", "model": "test-model", "api_key_env": None}}}


def bad(raw):
    with pytest.raises(ConfigError) as e:
        parse_config(raw)
    return str(e.value)


def test_defaults_backend_none_and_not_active():
    c = parse_config({})
    assert c.speech.backend == "none" and c.speech.enabled is True and not c.speech.active
    assert c.speech.min_speech_seconds == 1.0 and c.speech.audio_format == "wav" and c.speech.keep_audio is False
    assert c.speech.endpoint.url == "https://api.openai.com/v1/audio/transcriptions"
    assert c.speech.endpoint.response_format == "verbose_json" and c.speech.endpoint.timestamp_granularities == ["segment"]
    assert c.speech.endpoint.api_key_env == "OPENAI_API_KEY"
    assert c.media.audio_features is True


def test_transcribe_speech_alias_is_folded_into_speech_enabled():
    c = parse_config({"media": {"transcribe_speech": False}})
    assert c.speech.enabled is False and c.media.transcribe_speech is None and not c.speech.active
    again = parse_config(c.to_dict())  # the effective config round-trips (project.yaml is re-read by the next run)
    assert again.speech.enabled is False and again.config_hash() == c.config_hash()
    assert parse_config({"media": {"transcribe_speech": True}, "speech": {"backend": "none"}}).speech.enabled is True


def test_alias_conflict_is_an_error_but_agreement_is_fine():
    msg = bad({"media": {"transcribe_speech": False}, "speech": {"enabled": True}})
    assert "media.transcribe_speech" in msg and "speech.enabled" in msg and "deprecated" in msg
    assert parse_config({"media": {"transcribe_speech": False}, "speech": {"enabled": False}}).speech.enabled is False


def test_local_only_rejects_remote_transcription_endpoint():
    raw = {"llm": LOCAL_LLM, "media": {"local_only": True}, "speech": {"backend": "openai_transcriptions"}}
    msg = bad(raw)
    assert "local_only" in msg and "api.openai.com" in msg and "speech.endpoint" in msg
    ok = {"llm": LOCAL_LLM, "media": {"local_only": True},
          "speech": {"backend": "openai_transcriptions", "endpoint": {"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "m"}}}
    c = parse_config(ok)
    assert c.speech.is_local and c.speech.endpoint.url == "http://127.0.0.1:8000/v1/audio/transcriptions"
    for host in ("http://localhost:9000", "http://[::1]:9000"):
        assert parse_config({**ok, "speech": {**ok["speech"], "endpoint": {**ok["speech"]["endpoint"], "base_url": host}}}).speech.is_local


def test_local_only_allows_command_backend_and_inactive_remote_config():
    cmd = {"backend": "command", "command": {"executable": "whisper-cli", "args": ["-f", "{input}", "-of", "{output_base}"]}}
    c = parse_config({"llm": LOCAL_LLM, "media": {"local_only": True}, "speech": cmd})
    assert c.speech.active and c.speech.is_local
    assert parse_config({"llm": LOCAL_LLM, "media": {"local_only": True}, "speech": {"backend": "none"}}).speech.active is False
    assert parse_config({"llm": LOCAL_LLM, "media": {"local_only": True}, "speech": {"enabled": False, "backend": "openai_transcriptions"}})


def test_endpoint_validation():
    assert "credential" in bad({"speech": {"endpoint": {"api_key": "x"}}})
    assert "plain http" in bad({"speech": {"endpoint": {"base_url": "http://speech.example.com"}}})
    assert parse_config({"speech": {"endpoint": {"base_url": "http://speech.example.com", "allow_insecure_http": True}}})
    assert "own option" in bad({"speech": {"endpoint": {"params": {"model": "x"}}}})
    assert "own option" in bad({"speech": {"endpoint": {"params": {"timestamp_granularities[]": "word"}}}})
    assert "scalar" in bad({"speech": {"endpoint": {"params": {"x": {"a": 1}}}}})
    assert "not a full URL" in bad({"speech": {"endpoint": {"path": "http://x/y"}}})
    assert parse_config({"speech": {"endpoint": {"path": "audio/transcribe"}}}).speech.endpoint.url.endswith("/audio/transcribe")
    assert "blank" in bad({"speech": {"endpoint": {"model": "  "}}})
    assert bad({"speech": {"language": "english please"}})


def test_command_validation():
    assert "executable" in bad({"speech": {"backend": "command"}})
    assert "args is empty" in bad({"speech": {"backend": "command", "command": {"executable": "x"}}})
    assert "{input}" in bad({"speech": {"command": {"args": ["{output_dir}"]}}})
    assert "{output_dir}" in bad({"speech": {"command": {"args": ["{input}"]}}})
    assert parse_config({"speech": {"backend": "command", "command": {"executable": "x", "args": ["{input}", "{output_base}"], "output_format": "json"}}})
