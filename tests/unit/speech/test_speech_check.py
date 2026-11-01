from __future__ import annotations

import json
import wave

from speech_helpers import speech_cfg

from trip_vlog.config import parse_config
from trip_vlog.llm import check as llm_check
from trip_vlog.llm.transport import HttpResponse, TransportConnectionError
from trip_vlog.speech import check as sc


class Fake:
    def __init__(self, item):
        self.item, self.requests = item, []

    def send(self, request):
        self.requests.append(request)
        if isinstance(self.item, BaseException):
            raise self.item
        return self.item


def cfg(**endpoint):
    return speech_cfg(speech={"endpoint": {"base_url": "https://asr.example.com/x?token=SECRETQ", "model": "test-model", "api_key_env": "SPEECH_KEY", **endpoint}})


ENV = {"SPEECH_KEY": "sk-speech-secret-999"}


def test_generated_tone_is_a_one_second_16k_mono_wav(tmp_path):
    p = sc.write_test_wav(tmp_path / "t.wav")
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes(), w.getsampwidth()) == (1, 16000, 16000, 2)


def test_reachable_endpoint_with_empty_text_is_ok_and_reports_without_secrets():
    tr = Fake(HttpResponse(200, {}, json.dumps({"text": "", "segments": []}).encode()))
    rows = sc.check_speech(cfg(), transport=tr, env=ENV, sleep=lambda s: None)
    r = rows[0]
    assert (r.endpoint, r.test, r.status, r.where, r.model) == ("speech", "transcribe", "ok", "remote", "test-model") and "0 segment" in r.note
    assert r.latency_ms is not None
    table = llm_check.format_table(rows)
    assert "SECRETQ" not in table and "sk-speech" not in table and "https://asr.example.com/x" in table and "all checks passed" in table
    req = tr.requests[0]
    assert req.headers["authorization"] == "Bearer sk-speech-secret-999" and b"RIFF" in req.body and b'name="model"\r\n\r\ntest-model' in req.body


def test_failures_are_rows_not_exceptions():
    r = sc.check_speech(cfg(), transport=Fake(HttpResponse(401, {}, b'{"error": {"message": "bad key sk-speech-secret-999"}}')), env=ENV)[0]
    assert r.status == "fail" and "SpeechAuthError" in r.error and "sk-speech" not in r.error
    r = sc.check_speech(cfg(max_retries=0), transport=Fake(TransportConnectionError("refused")), env=ENV)[0]
    assert r.status == "fail" and "cannot reach" in r.error
    r = sc.check_speech(cfg(), transport=Fake(HttpResponse(200, {}, b"{}")), env={})[0]
    assert r.status == "fail" and "SPEECH_KEY" in r.error and "SpeechConfigError" in r.error


def test_none_and_disabled_are_skipped_not_failed():
    for c in (parse_config({}), speech_cfg(speech={"enabled": False})):
        r = sc.check_speech(c)[0]
        assert r.status == "skipped" and r.ok and r.note
    assert "skipped" in llm_check.format_table(sc.check_speech(parse_config({})))


def test_local_server_needs_no_key():
    c = speech_cfg(speech={"endpoint": {"base_url": "http://127.0.0.1:8000", "model": "test-model", "api_key_env": None}})
    tr = Fake(HttpResponse(200, {}, json.dumps({"text": "x"}).encode()))
    r = sc.check_speech(c, transport=tr, env={})[0]
    assert r.status == "ok" and r.where == "local" and "authorization" not in tr.requests[0].headers and "coarse" in r.note
