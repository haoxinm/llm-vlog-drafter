from __future__ import annotations

import json

import pytest
from llm_helpers import img, FAKE_SDK, FakeClient, make_request, sdk_message

from trip_vlog.config import parse_config
from trip_vlog.llm import Gateway, ReplayProvider, ResponseStore, make_gateway
from trip_vlog.llm.types import LLMConfigError, LLMStoreError, ReplayMissError


def cfg(**media):
    return parse_config({"media": {"llm_model": "test-model", **media}})


def test_record_then_replay_round_trip(img, tmp_path):
    fixtures = tmp_path / "fixtures"
    client = FakeClient(sdk_message(model="test-model-dated", rid="req_rec"))
    rec = make_gateway(cfg(llm_fixture_dir=str(fixtures)), tmp_path / "ws", "vision", mode="record", env={}, sdk=FAKE_SDK, client=client)
    recorded = rec.complete(make_request([img]))
    files = list(fixtures.glob("*.json"))
    assert [f.stem for f in files] == [recorded.fingerprint]
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["request"]["template_id"] == "media_analyst" and doc["request"]["template_version"] == "1"
    assert doc["response"]["model"] == "test-model-dated" and doc["response"]["usage"]["input_tokens"] == 11
    assert doc["response"]["parsed"] == {"label": "beach", "score": 7}
    assert "data" not in json.dumps(doc["request"]["parts"])  # hashes only, no image bytes

    # replay in a different workspace, no SDK, no client, no key
    rep = make_gateway(cfg(llm_fixture_dir=str(fixtures)), tmp_path / "other_ws", "vision", mode="replay", env={})
    out = rep.complete(make_request([img]))
    assert out.parsed == recorded.parsed and out.model == "test-model-dated" and out.request_id == "req_rec"
    assert out.usage == recorded.usage and out.fingerprint == recorded.fingerprint
    assert len(client.calls) == 1


def test_replay_miss_is_loud(img, tmp_path):
    gw = make_gateway(cfg(llm_fixture_dir=str(tmp_path / "empty")), tmp_path, "text", mode="replay", env={})
    req = make_request([img])
    with pytest.raises(ReplayMissError) as ei:
        gw.complete(req)
    assert ei.value.fingerprint == gw.fingerprint(req) and ei.value.template == "media_analyst.v1"
    assert ei.value.fingerprint in str(ei.value) and "media_analyst" in str(ei.value)


def test_replay_miss_when_prompt_version_or_image_changes(img, tmp_path):
    client = FakeClient(sdk_message())
    c = cfg(llm_fixture_dir=str(tmp_path / "f"))
    make_gateway(c, tmp_path, "text", mode="record", env={}, sdk=FAKE_SDK, client=client).complete(make_request([img]))
    rep = make_gateway(c, tmp_path, "text", mode="replay", env={})
    rep.complete(make_request([img]))
    with pytest.raises(ReplayMissError):
        rep.complete(make_request([img], version="2"))


def test_replay_miss_without_model_hints(img, tmp_path):
    gw = make_gateway(parse_config({"media": {"llm_provider": "replay", "vision_provider": "replay"}}), tmp_path, "text", env={})
    with pytest.raises(ReplayMissError, match="TRIP_VLOG_LLM_MODEL"):
        gw.complete(make_request([img]))


def test_replay_never_calls_a_provider_even_with_anthropic_configured(img, tmp_path):
    # env-selected replay with the default (anthropic) provider config: needs no SDK and no key
    gw = make_gateway(cfg(), tmp_path, "vision", env={"TRIP_VLOG_LLM_MODE": "replay"})
    assert isinstance(gw.provider, ReplayProvider)


def test_corrupt_fixture_is_an_error_in_replay(img, tmp_path):
    store = ResponseStore(tmp_path / "f")
    gw = Gateway(ReplayProvider(store, model="test-model"))
    fp = gw.fingerprint(make_request([img]))
    store.directory.mkdir()
    store.path_for(fp).write_text("{broken", encoding="utf-8")
    with pytest.raises(LLMStoreError):
        gw.complete(make_request([img]))


def test_workspace_cache_default_layout_and_reuse(img, tmp_path):
    client = FakeClient(sdk_message(), sdk_message())
    ws = tmp_path / "ws"
    gw = make_gateway(cfg(), ws, "text", env={}, sdk=FAKE_SDK, client=client)
    gw.complete(make_request([img]))
    assert len(list((ws / "cache" / "llm").glob("*.json"))) == 1
    assert (ws / "cache" / "llm" / "images").is_dir()  # prepared frames cached beside the responses
    again = make_gateway(cfg(), ws, "text", env={}, sdk=FAKE_SDK, client=client).complete(make_request([img]))
    assert again.cached and len(client.calls) == 1


def test_mode_off_ignores_fixture_dir_env_but_record_uses_env(img, tmp_path):
    client = FakeClient(sdk_message())
    env = {"TRIP_VLOG_LLM_MODE": "record", "TRIP_VLOG_LLM_FIXTURE_DIR": str(tmp_path / "envfx")}
    make_gateway(cfg(), tmp_path / "ws", "text", env=env, sdk=FAKE_SDK, client=client).complete(make_request([img]))
    assert len(list((tmp_path / "envfx").glob("*.json"))) == 1


def test_provider_selection_by_purpose(tmp_path):
    c = cfg(vision_provider="replay", llm_provider="anthropic")
    assert make_gateway(c, tmp_path, "vision", env={}).provider.name == "replay"
    assert make_gateway(c, tmp_path, "text", env={}, sdk=FAKE_SDK, client=FakeClient()).provider.name == "anthropic"


def test_local_only_blocks_cloud_provider(tmp_path):
    c = cfg(local_only=True, vision_provider="replay", llm_provider="replay")
    assert make_gateway(c, tmp_path, "text", env={}).provider.name == "replay"
    c2 = parse_config({"media": {"vision_provider": "replay", "llm_provider": "replay"}})
    c2.media.__dict__["llm_provider"] = "anthropic"  # bypass validation: the factory re-checks defensively
    c2.media.__dict__["local_only"] = True
    with pytest.raises(LLMConfigError, match="local_only"):
        make_gateway(c2, tmp_path, "text", env={}, sdk=FAKE_SDK, client=FakeClient())


def test_bad_mode_unknown_provider_and_record_with_replay_provider(tmp_path):
    with pytest.raises(LLMConfigError, match="TRIP_VLOG_LLM_MODE"):
        make_gateway(cfg(), tmp_path, "text", env={"TRIP_VLOG_LLM_MODE": "bogus"}, sdk=FAKE_SDK, client=FakeClient())
    with pytest.raises(LLMConfigError, match="unknown LLM provider"):
        make_gateway(cfg(llm_provider="mystery"), tmp_path, "text", env={})
    with pytest.raises(LLMConfigError, match="record mode needs a real provider"):
        make_gateway(cfg(llm_provider="replay"), tmp_path, "text", mode="record", env={})


def test_config_validation():
    from trip_vlog.config import ConfigError

    for bad in ({"llm_cache_dir": "/abs"}, {"llm_cache_dir": "../x"}, {"llm_cache_dir": "C:/x"}, {"llm_schema_retries": -1},
                {"llm_image_max_side": 10}, {"llm_model": "  "}, {"api_key": "sk-123"}):
        with pytest.raises(ConfigError):
            parse_config({"media": bad})
    m = parse_config({}).media
    assert (m.llm_max_retries, m.llm_schema_retries, m.llm_image_max_side, m.llm_cache_dir, m.llm_model) == (2, 2, 1568, "cache/llm", None)
