from __future__ import annotations

import json

import pytest
import yaml

from trip_vlog import cli
from trip_vlog.config import parse_config
from trip_vlog.llm import check
from trip_vlog.llm.transport import HttpResponse, TransportConnectionError

ENV = {"ANTHROPIC_API_KEY": "sk-ant-secret-key-123", "OPENAI_API_KEY": "sk-openai-secret-456"}


def msg(obj, model="test-model-resolved"):
    return HttpResponse(200, {}, json.dumps({"id": "m", "model": model, "stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(obj)}],
                                             "usage": {"input_tokens": 12, "output_tokens": 5}}).encode())


class Fake:
    """Routes by host so two endpoints can behave differently."""

    def __init__(self, by_host):
        self.by_host, self.requests = by_host, []

    def send(self, request):
        self.requests.append(request)
        item = self.by_host[request.url.split("/")[2]]
        item = item.pop(0) if isinstance(item, list) else item
        if isinstance(item, BaseException):
            raise item
        return item(request) if callable(item) else item


TEXT_OK = {"word": "pong", "number": 7}
IMG_OK = {"dominant_colour": "Red", "confidence": 0.9}


def two_endpoints():
    return parse_config({"llm": {"text": "good", "vision": "bad", "endpoints": {
        "good": {"api": "anthropic_messages", "model": "test-model", "base_url": "https://good.example.com/proxy?key=SECRETQ"},
        "bad": {"api": "openai_chat", "model": "test-model", "base_url": "https://bad.example.com", "structured_output": "json_prompt"}}}})


def test_all_ok_table_and_secrets_absent():
    cfg = two_endpoints()
    tr = Fake({"good.example.com": [msg(TEXT_OK), msg(IMG_OK)], "bad.example.com": [msg(TEXT_OK)]})
    # the second endpoint is openai_chat: answer in its shape
    chat = lambda obj: HttpResponse(200, {}, json.dumps({"model": "m", "choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}],
                                                          "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode())
    tr.by_host["bad.example.com"] = [chat(TEXT_OK), chat(IMG_OK)]
    rows = check.run_llm_check(cfg, transport=tr, env=ENV)
    assert [(r.endpoint, r.test, r.status) for r in rows] == [("good", "text", "ok"), ("good", "image", "ok"), ("bad", "text", "ok"), ("bad", "image", "ok")]
    table = check.format_table(rows)
    assert "SECRET" not in table and "sk-ant" not in table and "?" not in table
    for want in ("https://good.example.com/proxy", "remote", "test-model", "native", "json_prompt", "openai_chat", "12/5", "all checks passed"):
        assert want in table
    assert len(tr.requests) == 4
    body = json.loads(tr.requests[1].body)
    assert body["messages"][0]["content"][0]["type"] == "image"                   # the generated test image was sent


def test_failing_endpoint_row_and_unexpected_colour():
    cfg = two_endpoints()
    tr = Fake({"good.example.com": [msg(TEXT_OK), msg({"dominant_colour": "blue", "confidence": 0.5})],
               "bad.example.com": [HttpResponse(401, {}, b'{"error":{"message":"invalid key sk-ant-secret-key-123"}}')] * 2})
    rows = check.run_llm_check(cfg, transport=tr, env=ENV)
    by = {(r.endpoint, r.test): r for r in rows}
    assert by["good", "text"].ok and by["good", "image"].status == "fail" and by["good", "image"].error.startswith("UnexpectedAnswer")
    assert by["bad", "text"].status == "fail" and by["bad", "text"].error.startswith("LLMAuthError")
    table = check.format_table(rows)
    assert "FAIL" in table and "secret" not in table and "check(s) failed" in table


def test_connection_failure_missing_key_missing_model_and_no_image():
    cfg = parse_config({"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model", "max_retries": 0}}}})
    rows = check.run_llm_check(cfg, transport=Fake({"api.anthropic.com": TransportConnectionError("refused")}), env=ENV, include_image=False)
    assert len(rows) == 1 and rows[0].error.startswith("LLMConnectionError")
    rows = check.run_llm_check(cfg, env={})                                       # no key: never reaches the network
    assert rows[0].error.startswith("LLMConfigError") and "ANTHROPIC_API_KEY" in rows[0].error and rows[1].status == "skipped"
    nomodel = parse_config({"llm": {"endpoints": {"main": {"model": None, "model_env": "NO_SUCH_MODEL_VAR"}}}})
    assert "no model configured" in check.run_llm_check(nomodel, env=ENV)[0].error


def test_supports_images_false_skips_image_and_local_is_marked():
    cfg = parse_config({"llm": {"endpoints": {"main": {"api": "openai_chat", "base_url": "http://127.0.0.1:11434", "api_key_env": None,
                                                        "model": "m", "supports_images": False}}}})
    tr = Fake({"127.0.0.1:11434": [HttpResponse(200, {}, json.dumps({"model": "m", "choices": [{"message": {"content": json.dumps(TEXT_OK)}, "finish_reason": "stop"}]}).encode())]})
    rows = check.run_llm_check(cfg, transport=tr, env={})
    assert [r.status for r in rows] == ["ok", "skipped"] and rows[0].where == "local" and len(tr.requests) == 1


def test_named_endpoint_selection():
    cfg = two_endpoints()
    assert check.endpoints_to_check(cfg, None) == ["good", "bad"]
    assert check.endpoints_to_check(cfg, "bad") == ["bad"]
    with pytest.raises(Exception, match="unknown endpoint"):
        check.endpoints_to_check(cfg, "nope")


def _cli(monkeypatch, tmp_path, fake, *args, cfg=None):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(cfg or {"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}}}), encoding="utf-8")
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("trip_vlog.llm.http_provider.UrllibTransport", lambda: fake)
    return cli.main(["llm-check", "--config", str(path), *args])


def test_cli_exit_codes_and_no_fixture_or_cache_writes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TRIP_VLOG_LLM_MODE", "record")                            # must still be live and never record
    fake = Fake({"api.anthropic.com": [msg(TEXT_OK), msg(IMG_OK)]})
    assert _cli(monkeypatch, tmp_path, fake) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out and "endpoint" in out and "latency" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["project.yaml"]          # no cache/ or fixtures
    fake = Fake({"api.anthropic.com": [msg(TEXT_OK), HttpResponse(500, {}, b"{}"), HttpResponse(500, {}, b"{}"), HttpResponse(500, {}, b"{}")]})
    assert _cli(monkeypatch, tmp_path, fake) == 1
    assert "FAIL" in capsys.readouterr().out
    assert _cli(monkeypatch, tmp_path, Fake({}), "--endpoint", "nope") == 2
    fake = Fake({"api.anthropic.com": [msg(TEXT_OK)]})
    assert _cli(monkeypatch, tmp_path, fake, "--no-image") == 0 and len(fake.requests) == 1
