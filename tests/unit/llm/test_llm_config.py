from __future__ import annotations

import pytest

from trip_vlog.config import ConfigError, is_loopback_host, parse_config


def ep(**kw):
    return {"llm": {"endpoints": {"main": kw}}}


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("127.5.6.7", True), ("localhost", True), ("LOCALHOST", True), ("::1", True),
                                           ("[::1]", True), ("api.openai.com", False), ("192.168.1.5", False), ("0.0.0.0", False),
                                           ("127.0.0.1.evil.com", False), ("", False), (None, False)])
def test_loopback_detection(host, expected):
    assert is_loopback_host(host) is expected


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://127.9.9.9", "http://localhost:11434", "http://[::1]:1234", "https://localhost"])
def test_local_only_accepts_loopback(url):
    c = parse_config({"media": {"local_only": True}, **ep(api="openai_chat", base_url=url, api_key_env=None)})
    assert c.llm.endpoints["main"].is_local


@pytest.mark.parametrize("url", ["https://api.openai.com", "https://my-lan-box.example", "http://192.168.1.10:8000"])
def test_local_only_rejects_remote(url):
    with pytest.raises(ConfigError, match="local_only"):
        parse_config({"media": {"local_only": True}, **ep(api="openai_chat", base_url=url, allow_insecure_http=True)})


def test_local_only_checks_the_profiles_actually_used():
    raw = {"media": {"local_only": True}, "llm": {"text": "loc", "vision": "loc", "endpoints": {
        "loc": {"api": "openai_chat", "base_url": "http://localhost:8080", "api_key_env": None},
        "cloud": {"api": "openai_chat"}}}}
    assert parse_config(raw).media.local_only  # the unused remote profile is fine
    raw["llm"]["vision"] = "cloud"
    with pytest.raises(ConfigError, match="llm.vision.*'cloud'"):
        parse_config(raw)


def test_remote_http_refused_unless_allowed():
    with pytest.raises(ConfigError, match="plain http"):
        parse_config(ep(base_url="http://models.example.com"))
    assert parse_config(ep(base_url="http://models.example.com", allow_insecure_http=True)).llm.endpoints["main"].allow_insecure_http
    parse_config(ep(base_url="http://127.0.0.1:9"))  # loopback http is fine


@pytest.mark.parametrize("bad", [
    {"api_key": "sk-123"}, {"apiKey": "x"}, {"token": "x"}, {"api": "grpc"}, {"base_url": "ftp://x"}, {"base_url": "not a url"},
    {"base_url": "https://user:pw@host.example"}, {"path": "https://x/y"}, {"params": {"model": "x"}}, {"params": {"messages": []}},
    {"params": {"input": 1}}, {"params": {"system": "x"}}, {"params": {"instructions": "x"}}, {"params": {"stream": True}}, {"params": {"max_tokens": 5}},
    {"params": {"max_completion_tokens": 5}}, {"params": {"response_format": {}}}, {"params": {"text": {"format": {}}}},
    {"params": {"output_config": {"format": {}}}}, {"max_tokens_field": "token_limit", "params": {"token_limit": 5}},
    {"params": {"when": __import__("datetime").date(2026, 1, 2)}}, {"timeout_seconds": 0}, {"max_retries": -1},
    {"model": "  "}, {"structured_output": "magic"}, {"headers": {"bad name": "x"}}, {"api_key_env": "has space"}, {"nonsense": 1},
])
def test_endpoint_validation(bad):
    with pytest.raises(ConfigError):
        parse_config(ep(**bad))


def test_api_key_error_explains_env_var():
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_config(ep(api_key="sk-123"))


def test_defaults_per_api_and_explicit_null_key():
    d = parse_config({}).llm.endpoints["main"]
    assert (d.api, d.base_url, d.api_key_env, d.model, d.model_env, d.structured_output) == (
        "anthropic_messages", "https://api.anthropic.com", "ANTHROPIC_API_KEY", None, "TRIP_VLOG_LLM_MODEL", "native")
    o = parse_config(ep(api="openai_responses")).llm.endpoints["main"]
    assert (o.base_url, o.api_key_env) == ("https://api.openai.com", "OPENAI_API_KEY")
    n = parse_config(ep(api="openai_chat", api_key_env=None)).llm.endpoints["main"]
    assert n.api_key_env is None
    assert parse_config(ep(path="v1/x")).llm.endpoints["main"].path == "/v1/x"


def test_unknown_profile_and_legacy_media_keys():
    with pytest.raises(ConfigError, match="unknown endpoint profile 'nope'"):
        parse_config({"llm": {"text": "nope"}})
    for key in ("llm_provider", "vision_provider", "llm_model"):
        with pytest.raises(ConfigError, match="llm:"):
            parse_config({"media": {key: "x"}})


def test_cache_dir_must_stay_in_workspace():
    for bad in ("/abs", "../x", "C:/x"):
        with pytest.raises(ConfigError):
            parse_config({"llm": {"cache_dir": bad}})
    assert parse_config({}).llm.cache_dir == "cache/llm"


def test_config_hash_is_stable_through_yaml_round_trip():
    import yaml

    c = parse_config(ep(api="openai_chat", api_key_env=None, base_url="http://localhost:1"))
    assert parse_config(yaml.safe_load(c.to_yaml())).config_hash() == c.config_hash()


def test_reserved_param_error_names_the_key_and_unserializable_is_a_config_error():
    with pytest.raises(ConfigError, match="stream"):
        parse_config(ep(params={"stream": True}))
    with pytest.raises(ConfigError, match=r"llm.endpoints.main.params.when \(date\)"):
        parse_config(ep(params={"when": __import__("datetime").date(2026, 1, 2)}))
    parse_config(ep(params={"text": {"verbosity": "low"}, "output_config": {"effort": "low"}, "temperature": 0}))  # non-format sub-fields are fine
