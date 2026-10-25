from __future__ import annotations

import json
import math

import pytest

from trip_vlog.speech import parse as P
from trip_vlog.speech.multipart import encode_multipart
from trip_vlog.speech.types import SpeechProviderError

# ---------------------------------------------------------------- multipart


def test_multipart_exact_layout_and_file_bytes():
    payload = bytes(range(256)) + b"\r\n--not-the-boundary\r\n" + b"\x00\xff"
    body, ctype = encode_multipart([("model", "test-model"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "segment")],
                                   file_field="file", filename="audio.wav", content=payload, content_type="audio/wav", boundary="BOUNDARY123")
    assert ctype == "multipart/form-data; boundary=BOUNDARY123"
    expected = (b'--BOUNDARY123\r\nContent-Disposition: form-data; name="model"\r\n\r\ntest-model\r\n'
                b'--BOUNDARY123\r\nContent-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
                b'--BOUNDARY123\r\nContent-Disposition: form-data; name="timestamp_granularities[]"\r\n\r\nsegment\r\n'
                b'--BOUNDARY123\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'
                + payload + b"\r\n--BOUNDARY123--\r\n")
    assert body == expected


def test_multipart_random_boundary_never_occurs_in_payload_and_collision_is_rejected():
    body, ctype = encode_multipart([("a", "b")], file_field="file", filename="x.wav", content=b"abc")
    boundary = ctype.split("boundary=")[1]
    assert boundary.encode() not in b"abc" and body.count(f"--{boundary}".encode()) == 3  # two parts + closing
    other, ctype2 = encode_multipart([("a", "b")], file_field="file", filename="x.wav", content=b"abc")
    assert ctype != ctype2
    with pytest.raises(ValueError, match="boundary"):
        encode_multipart([], file_field="file", filename="x.wav", content=b"xx--B--xx", boundary="B")
    with pytest.raises(ValueError, match="boundary"):
        encode_multipart([("k", "has B inside")], file_field="file", filename="x.wav", content=b"", boundary="B")


def test_multipart_header_injection_is_neutralised():
    body, _ = encode_multipart([('na"me\r\nX: y', "v")], file_field="file", filename='a"\r\nb.wav', content=b"", content_type="audio/wav\r\nX: z",
                               boundary="B")
    head = body.split(b"\r\n\r\n")[0] + body.split(b"\r\n\r\n")[1]
    assert b"\r\nX:" not in body and b'name="na\\"meX: y"' in body and b'filename="a\\"b.wav"' in body and b"audio/wavX: z" in head


# ---------------------------------------------------------------- verbose_json


def test_verbose_json_segments_language_and_confidence():
    data = {"text": "hello world", "language": "english", "duration": 5.0, "segments": [
        {"id": 0, "start": 0.0, "end": 1.5, "text": " hello", "avg_logprob": -0.1, "no_speech_prob": 0.01},
        {"id": 1, "start": 1.5, "end": 9.0, "text": " world ", "avg_logprob": -0.5},
        {"id": 2, "start": 2.0, "end": 3.0, "text": "   "},                       # blank -> dropped
        {"id": 3, "start": "x", "end": 3.0, "text": "bad times"},                   # unusable times -> dropped
    ]}
    t = P.parse_verbose_json(data, 5.0)
    assert t.timestamps == "segment" and t.language == "english"
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 1.5, "hello"), (1.5, 5.0, "world")]   # end clamped to the clip
    assert t.segments[0].confidence == round(math.exp(-0.1), 4) and t.segments[0].no_speech_prob == 0.01 and t.segments[0].avg_logprob == -0.1
    assert t.segments[1].no_speech_prob is None


def test_text_only_reply_is_one_coarse_segment_spanning_the_clip():
    t = P.parse_verbose_json({"text": "  just text  "}, 12.5)
    assert t.timestamps == "coarse" and [(s.start, s.end, s.text, s.confidence) for s in t.segments] == [(0.0, 12.5, "just text", None)]
    t = P.parse_response_body(b"plain words\n", "text", 4.0)
    assert t.timestamps == "coarse" and t.segments[0].text == "plain words" and t.segments[0].end == 4.0
    t = P.parse_response_body(json.dumps({"text": "json format"}).encode(), "json", 3.0)
    assert t.timestamps == "coarse" and t.segments[0].text == "json format"


def test_empty_speech_is_a_valid_empty_transcript():
    assert P.parse_verbose_json({"text": "", "segments": []}, 3.0).segments == []
    assert P.parse_verbose_json({"text": "  "}, 3.0).segments == []


def test_unexpected_reply_shapes_raise():
    for bad in ([], "x", {"foo": 1}):
        with pytest.raises(SpeechProviderError):
            P.parse_verbose_json(bad, 1.0)
    with pytest.raises(SpeechProviderError, match="non-JSON"):
        P.parse_response_body(b"<html>", "verbose_json", 1.0)


# ---------------------------------------------------------------- SRT / VTT / JSON files

SRT = """﻿1
00:00:00,500 --> 00:00:02,000
Hello <i>there</i>,
world

2
00:01:02,250 --> 00:01:03,000
second   cue

3
00:02:00,000 --> 00:02:01,000

"""
VTT = """WEBVTT - made by a tool

NOTE a comment
that spans lines

00:00.500 --> 00:02.000 align:start position:0%
Hello <c.yellow>there</c>

cue-2
1:02:03.004 --> 1:02:04.000
later words
"""


def test_srt():
    segs = P.parse_srt(SRT, None)
    assert [(s.start, s.end, s.text) for s in segs] == [(0.5, 2.0, "Hello there, world"), (62.25, 63.0, "second cue")]


def test_srt_crlf_and_clamping():
    segs = P.parse_srt(SRT.replace("\n", "\r\n"), 1.0)
    assert [(s.start, s.end) for s in segs] == [(0.5, 1.0), (1.0, 1.0)]


def test_vtt_header_notes_settings_and_hours():
    segs = P.parse_vtt(VTT, None)
    assert [(s.start, s.end, s.text) for s in segs] == [(0.5, 2.0, "Hello there"), (3723.004, 3724.0, "later words")]


def test_whisper_style_json_and_whisper_cpp_json():
    w = {"text": "a b", "language": "en", "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": " a", "avg_logprob": -0.2, "no_speech_prob": 0.1},
                                                       {"id": 1, "start": 1.0, "end": 2.0, "text": " b"}]}
    t = P.parse_transcript_file(json.dumps(w), "json", 10.0)
    assert t.language == "en" and [s.text for s in t.segments] == ["a", "b"] and t.segments[0].confidence is not None
    cpp = {"result": {"language": "de"}, "transcription": [
        {"timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"}, "offsets": {"from": 0, "to": 1500}, "text": " Hallo"},
        {"timestamps": {"from": "00:00:01,500", "to": "00:00:02,000"}, "text": " Welt"}]}
    t = P.parse_transcript_file(json.dumps(cpp), "json", None)
    assert t.language == "de" and [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 1.5, "Hallo"), (1.5, 2.0, "Welt")]
    with pytest.raises(SpeechProviderError):
        P.parse_transcript_file("not json", "json", None)


def test_merge_shifts_chunks_and_marks_coarse():
    a = P.parse_verbose_json({"segments": [{"start": 0, "end": 1, "text": "one"}], "language": "en"}, 5)
    b = P.parse_verbose_json({"text": "two"}, 4)
    m = P.merge([(0.0, a), (600.0, b)])
    assert [(s.start, s.end, s.text) for s in m.segments] == [(0.0, 1.0, "one"), (600.0, 604.0, "two")]
    assert m.timestamps == "coarse" and m.language == "en"
