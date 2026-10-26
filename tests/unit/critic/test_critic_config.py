"""Configuration and endpoint profile of the critic (no network, no FilmCraft)."""
from __future__ import annotations

import pytest
import yaml
from critic_helpers import CriticTransport, critic_config, gateway_for

from trip_vlog.config import ConfigError, parse_config
from trip_vlog.llm.check import endpoints_to_check


def test_defaults_modes_and_limits():
    q = parse_config({}).qc
    assert (q.critic, q.critic_max_rounds, q.critic_max_ops_per_round, q.critic_apply_min_severity, q.critic_images) == ("off", 1, 3, "major", True)
    for mode in ("report", "apply"):
        assert parse_config({"qc": {"critic": mode}}).qc.critic == mode
    for bad in ({"critic": "auto"}, {"critic": True}, {"critic_max_rounds": 0}, {"critic_max_rounds": 9}, {"critic_max_ops_per_round": 0}, {"critic_apply_min_severity": "info"},
                {"critic_max_sheets": 0}, {"critic_unknown": 1}):
        with pytest.raises(ConfigError):
            parse_config({"qc": bad})


def test_an_unquoted_off_in_yaml_or_on_the_command_line_means_off():
    assert yaml.safe_load("qc: {critic: off}")["qc"]["critic"] is False       # the YAML 1.1 trap
    assert parse_config(yaml.safe_load("qc: {critic: off}")).qc.critic == "off"
    assert parse_config({}, {"qc.critic": yaml.safe_load("off")}).qc.critic == "off"
    assert parse_config({}, {"qc.critic": yaml.safe_load("apply")}).qc.critic == "apply"


def test_the_critic_profile_follows_the_existing_purpose_pattern():
    eps = {"main": {"api": "anthropic_messages", "model": "m"}, "vis": {"api": "openai_chat", "model": "v"}, "txt": {"api": "openai_chat", "model": "t"},
           "crit": {"api": "openai_responses", "model": "c"}}
    cfg = parse_config({"llm": {"endpoints": eps, "text": "txt", "vision": "vis"}})
    assert cfg.llm.critic is None
    assert cfg.llm.endpoint_for("critic")[0] == "vis" and cfg.llm.endpoint_for("critic", images=False)[0] == "txt"      # falls back to vision, or text without images
    assert cfg.llm.endpoint_for("text")[0] == "txt" and cfg.llm.endpoint_for("vision")[0] == "vis"
    cfg2 = parse_config({"llm": {"endpoints": eps, "text": "txt", "vision": "vis", "critic": "crit"}})
    assert cfg2.llm.endpoint_for("critic")[0] == cfg2.llm.endpoint_for("critic", images=False)[0] == "crit"
    with pytest.raises(ConfigError, match="llm.critic refers to unknown endpoint profile 'nope'"):
        parse_config({"llm": {"endpoints": eps, "critic": "nope"}})


def test_the_critic_gateway_uses_its_own_profile_and_model(tmp_path):
    cfg = critic_config(llm={"endpoints": {"main": {"api": "anthropic_messages", "model": "main-model"}, "crit": {"api": "anthropic_messages", "model": "critic-model"}},
                             "critic": "crit"})
    t = CriticTransport()
    gw = gateway_for(cfg, tmp_path, t)
    from trip_vlog.llm.types import LLMRequest, TextPart
    from trip_vlog.qc.critic import response_schema

    gw.complete(LLMRequest("critic", "1", "sys", (TextPart("hi"),), response_schema(), 100))
    assert t.calls[0].body["model"] == "critic-model" and gw.model == "critic-model"
    cfg_default = critic_config()
    t2 = CriticTransport()
    gateway_for(cfg_default, tmp_path / "other", t2).complete(LLMRequest("critic", "1", "sys", (TextPart("hi"),), response_schema(), 100))
    assert t2.calls[0].body["model"] == "test-model"


def test_local_only_requires_a_loopback_critic_endpoint_only_when_the_critic_is_on():
    eps = {"main": {"api": "openai_chat", "base_url": "http://127.0.0.1:9", "model": "m", "api_key_env": None},
           "remote": {"api": "anthropic_messages", "model": "r"}}
    base = {"media": {"local_only": True}, "llm": {"endpoints": eps, "text": "main", "vision": "main", "critic": "remote"}}
    parse_config(base)                                                                    # critic off: not used, not checked
    with pytest.raises(ConfigError, match="llm.critic uses endpoint 'remote'"):
        parse_config({**base, "qc": {"critic": "report"}})
    parse_config({**base, "llm": {**base["llm"], "critic": None}, "qc": {"critic": "report"}})   # falls back to the local vision profile


def test_llm_check_includes_the_critic_profile_when_it_is_on():
    eps = {"main": {"api": "anthropic_messages", "model": "m"}, "crit": {"api": "openai_chat", "model": "c"}}
    off = parse_config({"llm": {"endpoints": eps, "critic": "crit"}})
    on = parse_config({"llm": {"endpoints": eps, "critic": "crit"}, "qc": {"critic": "report"}})
    assert endpoints_to_check(off, None) == ["main"] and endpoints_to_check(on, None) == ["main", "crit"]
