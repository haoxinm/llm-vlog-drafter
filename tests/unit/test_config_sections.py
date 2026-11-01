"""Config sections that users commonly leave blank, and separate providers for LLM and speech."""

from pathlib import Path

import pytest

from trip_vlog.config import ConfigError, load_config


def _load(tmp_path: Path, text: str):
    p = tmp_path / "project.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


@pytest.mark.parametrize("speech_text", ["", "speech:\n", "speech: {}\n", "speech:\n  backend:\n", "speech:\n  backend: ''\n"])
def test_blank_speech_section_skips_transcription(tmp_path, speech_text):
    c = _load(tmp_path, "project: {title: T}\n" + speech_text)
    assert c.speech.backend == "none"


def test_blank_llm_section_uses_defaults(tmp_path):
    c = _load(tmp_path, "project: {title: T}\nllm:\n")
    assert c.llm.text in c.llm.endpoints and c.llm.vision in c.llm.endpoints


def test_anthropic_for_llm_and_openai_for_speech(tmp_path):
    c = _load(tmp_path, """
project: {title: T}
llm:
  text: main
  vision: main
  endpoints:
    main: {api: anthropic_messages, model: test-model}
speech:
  backend: openai_transcriptions
  endpoint: {base_url: "https://api.openai.com", model: test-speech-model}
""")
    main = c.llm.endpoints["main"]
    assert (main.api, main.api_key_env) == ("anthropic_messages", "ANTHROPIC_API_KEY")
    assert c.speech.backend == "openai_transcriptions"
    assert c.speech.endpoint.api_key_env == "OPENAI_API_KEY"
    assert c.speech.endpoint.model == "test-speech-model"


def test_local_only_checks_speech_endpoint_separately(tmp_path):
    base = """
project: {title: T}
media: {local_only: true}
llm:
  endpoints:
    main: {api: openai_chat, base_url: "http://127.0.0.1:8080", api_key_env: null, model: test-model}
"""
    ok = _load(tmp_path, base + "speech:\n  backend: none\n")
    assert ok.speech.backend == "none"
    with pytest.raises(ConfigError):
        _load(tmp_path, base + "speech:\n  backend: openai_transcriptions\n  endpoint: {base_url: \"https://api.openai.com\"}\n")
