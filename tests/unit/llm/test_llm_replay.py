from __future__ import annotations

import json

import pytest
from llm_helpers import FakeTransport, anthropic_reply, chat_reply, img, make_request  # noqa: F401

from trip_vlog.config import parse_config
from trip_vlog.llm import Gateway, ReplayProvider, ResponseStore, make_gateway
from trip_vlog.llm.types import LLMConfigError, LLMStoreError, ReplayMissError

ENV = {"ANTHROPIC_API_KEY": "k-123456"}


def cfg(**llm):
    return parse_config({"llm": {"endpoints": {"main": {"model": "test-model"}}, **llm}})


def test_record_then_replay_round_trip(img, tmp_path):
    fixtures = tmp_path / "fixtures"
    t = FakeTransport(anthropic_reply(model="test-model-dated"))
    rec = make_gateway(cfg(fixture_dir=str(fixtures)), tmp_path / "ws", "vision", mode="record", env=ENV, transport=t)
    recorded = rec.complete(make_request([img]))
    (f,) = list(fixtures.glob("*.json"))
    assert f.stem == recorded.fingerprint
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert doc["request"]["template_id"] == "media_analyst" and doc["request"]["template_version"] == "1"
    assert doc["request"]["provider"] == {"api": "anthropic_messages", "params": {}}
    assert doc["response"]["model"] == "test-model-dated" and doc["response"]["usage"]["input_tokens"] == 11
    assert doc["response"]["parsed"] == {"label": "beach", "score": 7}
    assert "data" not in json.dumps(doc["request"]["parts"]) and "https://" not in f.read_text()  # hashes only, no URLs

    rep = make_gateway(cfg(fixture_dir=str(fixtures)), tmp_path / "other_ws", "vision", mode="replay", env={})  # no key, no transport
    out = rep.complete(make_request([img]))
    assert out.parsed == recorded.parsed and out.model == "test-model-dated" and out.fingerprint == recorded.fingerprint
    assert out.usage == recorded.usage and len(t.requests) == 1


def test_record_against_one_host_replays_against_another(img, tmp_path):
    fx = str(tmp_path / "fx")
    a = parse_config({"llm": {"fixture_dir": fx, "endpoints": {"main": {"model": "test-model", "base_url": "https://one.example", "path": "/x", "timeout_seconds": 5,
                                                                          "headers": {"X-A": "1"}}}}})
    b = parse_config({"llm": {"fixture_dir": fx, "endpoints": {"main": {"model": "test-model", "base_url": "https://two.example", "timeout_seconds": 99}}}})
    make_gateway(a, tmp_path, "text", mode="record", env=ENV, transport=FakeTransport(anthropic_reply())).complete(make_request([img]))
    assert make_gateway(b, tmp_path, "text", mode="replay", env={}).complete(make_request([img])).parsed["label"] == "beach"


def test_params_and_api_change_the_fingerprint(img, tmp_path):
    def fp(**ep):
        c = parse_config({"llm": {"endpoints": {"main": {"model": "test-model", **ep}}}})
        return make_gateway(c, tmp_path, "text", mode="replay", env={}).fingerprint(make_request([img]))

    base = fp()
    assert fp(params={"temperature": 0}) != base
    assert fp(params={"temperature": 0}) != fp(params={"temperature": 1})
    assert fp(api="openai_chat") != base
    assert fp(base_url="https://elsewhere.example", timeout_seconds=7, headers={"X": "y"}, path="/z") == base


def test_replay_miss_is_loud(img, tmp_path):
    gw = make_gateway(cfg(fixture_dir=str(tmp_path / "empty")), tmp_path, "text", mode="replay", env={})
    req = make_request([img])
    with pytest.raises(ReplayMissError) as ei:
        gw.complete(req)
    assert ei.value.fingerprint == gw.fingerprint(req) and ei.value.template == "media_analyst.v1"
    assert ei.value.fingerprint in str(ei.value) and "media_analyst" in str(ei.value)


def test_replay_miss_when_prompt_version_changes(img, tmp_path):
    c = cfg(fixture_dir=str(tmp_path / "f"))
    make_gateway(c, tmp_path, "text", mode="record", env=ENV, transport=FakeTransport(anthropic_reply())).complete(make_request([img]))
    rep = make_gateway(c, tmp_path, "text", mode="replay", env={})
    rep.complete(make_request([img]))
    with pytest.raises(ReplayMissError):
        rep.complete(make_request([img], version="2"))


def test_replay_miss_without_model_hints(img, tmp_path):
    gw = make_gateway(parse_config({}), tmp_path, "text", mode="replay", env={})
    with pytest.raises(ReplayMissError, match="model_env|model"):
        gw.complete(make_request([img]))


def test_mode_from_env_overrides_config(img, tmp_path):
    c = cfg(mode="live")
    assert isinstance(make_gateway(c, tmp_path, "vision", env={"TRIP_VLOG_LLM_MODE": "replay"}).provider, ReplayProvider)
    assert make_gateway(cfg(mode="replay"), tmp_path, "vision", env={}).provider.name == "replay"
    g = make_gateway(c, tmp_path, "vision", env={**ENV, "TRIP_VLOG_LLM_MODE": "record", "TRIP_VLOG_LLM_FIXTURE_DIR": str(tmp_path / "envfx")},
                     transport=FakeTransport(anthropic_reply()))
    g.complete(make_request([img]))
    assert len(list((tmp_path / "envfx").glob("*.json"))) == 1
    with pytest.raises(LLMConfigError, match="TRIP_VLOG_LLM_MODE"):
        make_gateway(c, tmp_path, "vision", env={"TRIP_VLOG_LLM_MODE": "bogus"})


def test_corrupt_fixture_is_an_error_in_replay(img, tmp_path):
    store = ResponseStore(tmp_path / "f")
    gw = Gateway(ReplayProvider(store, model="test-model"))
    fp = gw.fingerprint(make_request([img]))
    store.directory.mkdir()
    store.path_for(fp).write_text("{broken", encoding="utf-8")
    with pytest.raises(LLMStoreError):
        gw.complete(make_request([img]))


def test_workspace_cache_default_layout_and_reuse(img, tmp_path):
    t = FakeTransport(anthropic_reply(), anthropic_reply())
    ws = tmp_path / "ws"
    make_gateway(cfg(), ws, "text", env=ENV, transport=t).complete(make_request([img]))
    assert len(list((ws / "cache" / "llm").glob("*.json"))) == 1
    assert (ws / "cache" / "llm" / "images").is_dir()
    again = make_gateway(cfg(), ws, "text", env=ENV, transport=t).complete(make_request([img]))
    assert again.cached and len(t.requests) == 1


def test_cache_key_changes_with_prompt_version_and_model(img, tmp_path):
    t = FakeTransport(anthropic_reply(), anthropic_reply(), anthropic_reply())
    g = make_gateway(cfg(), tmp_path, "text", env=ENV, transport=t)
    g.complete(make_request([img]))
    g.complete(make_request([img], version="2"))
    other = make_gateway(parse_config({"llm": {"endpoints": {"main": {"model": "test-model-b"}}}}), tmp_path, "text", env=ENV, transport=t)
    other.complete(make_request([img]))
    assert len(t.requests) == 3


def test_cache_hit_avoids_http_call_and_replay_never_needs_transport(img, tmp_path):
    t = FakeTransport(chat_reply())
    c = parse_config({"llm": {"endpoints": {"main": {"api": "openai_chat", "model": "test-model", "base_url": "http://localhost:1", "api_key_env": None}}}})
    g = make_gateway(c, tmp_path, "text", env={}, transport=t)
    g.complete(make_request([img]))
    assert g.complete(make_request([img])).cached and len(t.requests) == 1
