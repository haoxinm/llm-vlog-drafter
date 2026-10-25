from __future__ import annotations

import json
import sys
import textwrap

import pytest

from trip_vlog.config import SpeechCommandConfig, SpeechEndpointConfig, parse_config
from trip_vlog.llm.transport import HttpResponse, TransportConnectionError, TransportTimeout
from trip_vlog.speech import command_backend as cb
from trip_vlog.speech.http_backend import OpenAITranscriptions, form_fields
from trip_vlog.speech.types import (
    SpeechAuthError,
    SpeechBadRequestError,
    SpeechCommandError,
    SpeechConfigError,
    SpeechConnectionError,
    SpeechNotFoundError,
    SpeechProviderError,
    SpeechRateLimitError,
    SpeechTimeoutError,
)

ENV = {"OPENAI_API_KEY": "sk-speech-secret-123", "TRIP_VLOG_SPEECH_MODEL": "env-model"}
OK = {"text": "hi there", "language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": "hi there", "avg_logprob": -0.2}]}


def ok(obj=OK):
    return HttpResponse(200, {}, json.dumps(obj).encode())


class Fake:
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    def send(self, request):
        self.requests.append(request)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


def ep(**kw):
    return SpeechEndpointConfig(model="test-model", **kw)


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "clip.wav"
    p.write_bytes(b"RIFF....WAVEfake-audio-bytes")
    return p


def backend(tr, endpoint=None, env=ENV, language=None, sleeps=None):
    return OpenAITranscriptions(endpoint or ep(), language=language, env=env, transport=tr,
                                sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# ---------------------------------------------------------------- HTTP: request shape


def test_request_shape_url_auth_fields_and_file(wav):
    tr = Fake(ok())
    t = backend(tr, ep(params={"temperature": 0, "prompt": "trip to Kyoto", "flag": True, "multi": ["a", "b"]}), language="de").transcribe(wav, 5.0)
    assert t.segments[0].text == "hi there" and t.language == "en"
    req = tr.requests[0]
    assert req.method == "POST" and req.url == "https://api.openai.com/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer sk-speech-secret-123"
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    boundary = req.headers["content-type"].split("boundary=")[1].encode()
    names = [p.split(b'name="')[1].split(b'"')[0].decode() for p in req.body.split(b"--" + boundary)[1:-1]]
    assert names == ["model", "response_format", "timestamp_granularities[]", "language", "temperature", "prompt", "flag", "multi", "multi", "file"]
    body = req.body
    assert b'name="model"\r\n\r\ntest-model\r\n' in body and b'name="response_format"\r\n\r\nverbose_json\r\n' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment\r\n' in body and b'name="language"\r\n\r\nde\r\n' in body
    assert b'name="temperature"\r\n\r\n0\r\n' in body and b'name="flag"\r\n\r\ntrue\r\n' in body and b'name="prompt"\r\n\r\ntrip to Kyoto\r\n' in body
    assert b'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF....WAVEfake-audio-bytes\r\n' in body
    assert str(wav.name).encode() not in body                      # the real file name never leaves the machine
    assert body.endswith(b"--" + boundary + b"--\r\n")


def test_form_fields_options():
    assert form_fields(ep(timestamp_granularities=[]), "m", None) == [("model", "m"), ("response_format", "verbose_json")]
    assert form_fields(ep(response_format="text"), "m", "fr") == [("model", "m"), ("response_format", "text"), ("language", "fr")]
    assert form_fields(ep(timestamp_granularities=["segment", "word"]), "m", None)[2:] == [("timestamp_granularities[]", "segment"),
                                                                                           ("timestamp_granularities[]", "word")]


def test_custom_path_no_auth_and_header_env_expansion(wav):
    tr = Fake(ok())
    e = ep(base_url="http://127.0.0.1:8080", path="/inference", api_key_env=None, headers={"X-Org": "org-${ORG_ID}"})
    backend(tr, e, env={"ORG_ID": "42"}).transcribe(wav, 1.0)
    r = tr.requests[0]
    assert r.url == "http://127.0.0.1:8080/inference" and "authorization" not in r.headers and r.headers["x-org"] == "org-42"


def test_model_from_env_and_missing_config_errors(wav):
    e = SpeechEndpointConfig()
    assert backend(Fake(ok()), e).model == "env-model"
    with pytest.raises(SpeechConfigError, match="model"):
        backend(Fake(ok()), e, env={"OPENAI_API_KEY": "sk-abcdef"})
    with pytest.raises(SpeechConfigError, match="OPENAI_API_KEY"):
        backend(Fake(ok()), ep(), env={})
    with pytest.raises(SpeechConfigError, match="ORG_ID"):
        backend(Fake(ok()), ep(headers={"X": "${ORG_ID}"}), env=ENV)


def test_text_only_server_reply_is_coarse(wav):
    t = backend(Fake(ok({"text": "only text"}))).transcribe(wav, 7.0)
    assert t.timestamps == "coarse" and (t.segments[0].start, t.segments[0].end) == (0.0, 7.0)


def test_response_format_srt_is_parsed(wav):
    tr = Fake(HttpResponse(200, {}, b"1\n00:00:00,000 --> 00:00:01,000\nsrt text\n"))
    t = backend(tr, ep(response_format="srt")).transcribe(wav, 3.0)
    assert t.segments[0].text == "srt text" and b'name="response_format"\r\n\r\nsrt\r\n' in tr.requests[0].body
    assert b"timestamp_granularities" not in tr.requests[0].body


# ---------------------------------------------------------------- HTTP: retries and error mapping


def test_retries_5xx_with_backoff_then_succeeds(wav):
    sleeps: list[float] = []
    tr = Fake(HttpResponse(503, {}, b"busy"), HttpResponse(429, {"retry-after": "3"}, b"slow"), ok())
    t = backend(tr, ep(max_retries=2), sleeps=sleeps).transcribe(wav, 1.0)
    assert t.segments and len(tr.requests) == 3 and sleeps == [0.5, 3.0]


def test_retries_exhausted_map_to_typed_errors(wav):
    with pytest.raises(SpeechRateLimitError, match="429"):
        backend(Fake(HttpResponse(429, {}, b'{"error": {"message": "quota"}}')), ep(max_retries=1)).transcribe(wav, 1.0)
    tr = Fake(HttpResponse(502, {}, b"bad gateway"))
    with pytest.raises(SpeechProviderError, match="502") as ei:
        backend(tr, ep(max_retries=2)).transcribe(wav, 1.0)
    assert len(tr.requests) == 3 and ei.value.status_code == 502


@pytest.mark.parametrize("status,exc", [(400, SpeechBadRequestError), (413, SpeechBadRequestError), (415, SpeechBadRequestError),
                                        (401, SpeechAuthError), (403, SpeechAuthError), (404, SpeechNotFoundError)])
def test_client_errors_are_not_retried(wav, status, exc):
    tr = Fake(HttpResponse(status, {}, json.dumps({"error": {"message": "nope sk-speech-secret-123"}}).encode()))
    with pytest.raises(exc) as ei:
        backend(tr, ep(max_retries=3)).transcribe(wav, 1.0)
    assert len(tr.requests) == 1 and ei.value.status_code == status
    assert "sk-speech-secret-123" not in str(ei.value)           # secrets echoed by a server are masked


def test_timeout_is_not_retried_and_connection_errors_are(wav):
    tr = Fake(TransportTimeout("t"))
    with pytest.raises(SpeechTimeoutError, match="timeout_seconds"):
        backend(tr, ep(max_retries=3, timeout_seconds=7)).transcribe(wav, 1.0)
    assert len(tr.requests) == 1 and tr.requests[0].timeout == 7
    tr = Fake(TransportConnectionError("ConnectionRefusedError: refused"))
    with pytest.raises(SpeechConnectionError, match="cannot reach"):
        backend(tr, ep(max_retries=2)).transcribe(wav, 1.0)
    assert len(tr.requests) == 3
    tr = Fake(TransportConnectionError("reset"), ok())
    assert backend(tr).transcribe(wav, 1.0).segments


def test_non_json_success_body_and_oversize_file(wav, monkeypatch):
    with pytest.raises(SpeechProviderError, match="non-JSON"):
        backend(Fake(HttpResponse(200, {}, b"<html>"))).transcribe(wav, 1.0)
    monkeypatch.setattr("trip_vlog.speech.http_backend.MAX_UPLOAD_BYTES", 5)
    with pytest.raises(SpeechBadRequestError, match="chunk_seconds"):
        backend(Fake(ok())).transcribe(wav, 1.0)


# ---------------------------------------------------------------- command backend

FAKE_TOOL = textwrap.dedent('''
    import json, pathlib, sys, time
    mode, inp, out_dir, lang = sys.argv[1:5]
    out = pathlib.Path(out_dir)
    assert pathlib.Path(inp).is_file(), "input missing"
    if mode == "srt":
        (out / "transcript.srt").write_text("1\\n00:00:00,000 --> 00:00:01,500\\nhello {lang}\\n".format(lang=lang), encoding="utf-8")
    elif mode == "vtt-named":      # tools that name the file after the input
        (out / (pathlib.Path(inp).stem + ".vtt")).write_text("WEBVTT\\n\\n00:00.000 --> 00:01.000\\nvtt words\\n", encoding="utf-8")
    elif mode == "json":
        (out / "transcript.json").write_text(json.dumps({"language": "en", "segments": [{"start": 0, "end": 2, "text": "json words", "avg_logprob": -0.3}]}))
    elif mode == "badjson":
        (out / "transcript.json").write_text("this is not json")
    elif mode == "fail":
        sys.stderr.write("model file not found"); sys.exit(3)
    elif mode == "sleep":
        time.sleep(30)
    elif mode == "nothing":
        pass
''')


@pytest.fixture
def tool(tmp_path):
    p = tmp_path / "fake_asr.py"
    p.write_text(FAKE_TOOL, encoding="utf-8")
    return p


def cmd(tool, mode, fmt="srt", timeout=60.0, language=None):
    cfg = SpeechCommandConfig(executable=sys.executable, args=[str(tool), mode, "{input}", "{output_dir}", "{language}"], output_format=fmt, timeout_seconds=timeout)
    return cb.CommandTranscriber(cfg, language=language)


def test_command_srt_with_language_placeholder(tool, wav):
    t = cmd(tool, "srt", language="fr").transcribe(wav, 5.0)
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 1.5, "hello fr")]
    assert cmd(tool, "srt").transcribe(wav, 5.0).segments[0].text == "hello auto"


def test_command_vtt_named_after_input_and_json(tool, wav):
    assert cmd(tool, "vtt-named", "vtt").transcribe(wav, 5.0).segments[0].text == "vtt words"
    t = cmd(tool, "json", "json").transcribe(wav, 5.0)
    assert t.language == "en" and t.segments[0].text == "json words" and t.segments[0].confidence is not None


def test_command_failure_modes(tool, wav):
    with pytest.raises(SpeechCommandError, match="status 3.*model file not found"):
        cmd(tool, "fail").transcribe(wav, 5.0)
    with pytest.raises(SpeechCommandError, match=r"wrote no \*\.srt"):
        cmd(tool, "nothing").transcribe(wav, 5.0)
    with pytest.raises(SpeechCommandError, match="did not finish within 0.5s"):
        cmd(tool, "sleep", timeout=0.5).transcribe(wav, 5.0)
    with pytest.raises(SpeechCommandError, match="cannot parse the json"):
        cmd(tool, "badjson", "json").transcribe(wav, 5.0)


def test_command_executable_resolution_and_no_shell(tmp_path):
    with pytest.raises(SpeechConfigError, match="not found"):
        cb.CommandTranscriber(SpeechCommandConfig(executable="definitely-not-installed-asr", args=["{input}", "{output_dir}"]))
    args = cb.expand_args(["-f", "{input}", "-of", "{output_base}", "--lang={language}; rm -rf /", "{output_dir}"], input_path=tmp_path / "a b.wav",
                          output_dir=tmp_path / "out", language=None)
    assert args == ["-f", str(tmp_path / "a b.wav"), "-of", str(tmp_path / "out" / "transcript"), "--lang=auto; rm -rf /", str(tmp_path / "out")]


def test_command_temp_dir_is_removed_and_input_untouched(tool, wav, tmp_path):
    seen = []
    orig = cb.find_output
    cb.find_output = lambda d, f: (seen.append(d), orig(d, f))[1]
    try:
        cmd(tool, "srt").transcribe(wav, 5.0)
    finally:
        cb.find_output = orig
    assert seen and not seen[0].exists() and wav.read_bytes().startswith(b"RIFF")
