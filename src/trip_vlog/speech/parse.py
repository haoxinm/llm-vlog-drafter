"""Parsers for transcript replies and files: OpenAI-style verbose_json, plain text, SRT, VTT and whisper-style JSON.

All parsers are pure and tolerant: unusable segments are dropped, never invented. Times are clamped to [0, duration] when
the clip duration is known.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Mapping

from trip_vlog.speech.types import SpeechProviderError, Transcription, TranscriptSegment, clean_float

_TAG_RE = re.compile(r"<[^>]*>")
_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_ARROW = re.compile(r"\s*-->\s*")
MAX_TEXT_CHARS = 4000


def _clean_text(text: Any) -> str:
    return " ".join(str(text or "").split())[:MAX_TEXT_CHARS]


def parse_timestamp(text: str) -> float | None:
    """'00:01:02,500' / '01:02.5' / '1:02:03.004' -> seconds."""
    m = _TIME_RE.fullmatch(text.strip())
    if not m:
        return None
    h, mi, s, frac = int(m[1] or 0), int(m[2]), int(m[3]), m[4]
    return h * 3600 + mi * 60 + s + int(frac.ljust(3, "0")) / 1000


def _segment(start: Any, end: Any, text: Any, duration: float | None, *, avg_logprob: Any = None,
             no_speech_prob: Any = None) -> TranscriptSegment | None:
    s, e, t = clean_float(start), clean_float(end), _clean_text(text)
    if s is None or e is None or not t:
        return None
    s = max(0.0, s)
    if duration is not None:
        s, e = min(s, duration), min(e, duration)
    e = max(e, s)
    lp, ns = clean_float(avg_logprob), clean_float(no_speech_prob)
    conf = round(min(1.0, max(0.0, math.exp(lp))), 4) if lp is not None and lp <= 0 else None
    return TranscriptSegment(round(s, 3), round(e, 3), t, conf, lp, ns)


def _coarse(text: Any, duration: float | None, language: str | None) -> Transcription:
    t = _clean_text(text)
    segs = [TranscriptSegment(0.0, round(duration or 0.0, 3), t)] if t else []
    return Transcription(segs, language, "coarse")


def _lang(v: Any) -> str | None:
    return v.strip()[:35] if isinstance(v, str) and v.strip() else None


def parse_verbose_json(data: Any, duration: float | None) -> Transcription:
    """OpenAI-style reply: {text, language, segments: [{start, end, text, avg_logprob, no_speech_prob}]}.
    A reply with only `text` (plain `json` format, or a server without segments) becomes one coarse segment."""
    if not isinstance(data, Mapping):
        raise SpeechProviderError("the transcription reply is not a JSON object")
    lang = _lang(data.get("language"))
    raw = data.get("segments")
    if isinstance(raw, list) and raw:
        segs = [x for x in (_segment(r.get("start"), r.get("end"), r.get("text"), duration, avg_logprob=r.get("avg_logprob"),
                                     no_speech_prob=r.get("no_speech_prob")) for r in raw if isinstance(r, Mapping)) if x]
        if segs:
            return Transcription(segs, lang, "segment")
        if not _clean_text(data.get("text")):
            return Transcription([], lang, "segment")
    if "text" not in data and not isinstance(raw, list):
        raise SpeechProviderError("the transcription reply has neither `segments` nor `text`; is the endpoint/response_format right?")
    return _coarse(data.get("text"), duration, lang)


def parse_srt(text: str, duration: float | None) -> list[TranscriptSegment]:
    return _parse_cues(text, duration, vtt=False)


def parse_vtt(text: str, duration: float | None) -> list[TranscriptSegment]:
    return _parse_cues(text, duration, vtt=True)


def _parse_cues(text: str, duration: float | None, *, vtt: bool) -> list[TranscriptSegment]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    out: list[TranscriptSegment] = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = [ln for ln in block.split("\n")]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue  # WEBVTT header, NOTE/STYLE blocks, stray numbers
        halves = _ARROW.split(lines[idx].strip(), maxsplit=1)
        if len(halves) != 2 or not halves[1].split():
            continue
        start, end = parse_timestamp(halves[0]), parse_timestamp(halves[1].split()[0])  # VTT cue settings follow the end time
        body = _TAG_RE.sub("", " ".join(lines[idx + 1:]))
        seg = _segment(start, end, body, duration)
        if seg:
            out.append(seg)
    return out


def _ms(v: Any) -> float | None:
    f = clean_float(v)
    return None if f is None else f / 1000.0


def parse_whisper_json(data: Any, duration: float | None) -> Transcription:
    """openai-whisper / faster-whisper style ({segments: [{start, end, text}]}) and whisper.cpp `-oj` style
    ({transcription: [{offsets: {from, to} (ms), timestamps: {from, to}, text}], result: {language}})."""
    if not isinstance(data, Mapping):
        raise SpeechProviderError("the transcript JSON is not an object")
    if isinstance(data.get("transcription"), list):
        segs = []
        for r in data["transcription"]:
            if not isinstance(r, Mapping):
                continue
            off, ts = r.get("offsets") or {}, r.get("timestamps") or {}
            start = _ms(off.get("from")) if isinstance(off, Mapping) and "from" in off else parse_timestamp(str(ts.get("from", ""))) if isinstance(ts, Mapping) else None
            end = _ms(off.get("to")) if isinstance(off, Mapping) and "to" in off else parse_timestamp(str(ts.get("to", ""))) if isinstance(ts, Mapping) else None
            seg = _segment(start, end, r.get("text"), duration)
            if seg:
                segs.append(seg)
        res = data.get("result")
        return Transcription(segs, _lang(res.get("language")) if isinstance(res, Mapping) else None, "segment")
    return parse_verbose_json(data, duration)


def parse_transcript_file(text: str, fmt: str, duration: float | None) -> Transcription:
    if fmt == "srt":
        return Transcription(parse_srt(text, duration))
    if fmt == "vtt":
        return Transcription(parse_vtt(text, duration))
    if fmt == "json":
        try:
            return parse_whisper_json(json.loads(text.lstrip("﻿")), duration)
        except ValueError:
            raise SpeechProviderError("the transcript file is not valid JSON") from None
    raise ValueError(f"unknown transcript format {fmt!r}")


def parse_response_body(body: bytes, response_format: str, duration: float | None) -> Transcription:
    """Reply of the HTTP backend in whatever `response_format` was requested."""
    text = body.decode("utf-8", "replace")
    if response_format in ("verbose_json", "json"):
        try:
            data = json.loads(text)
        except ValueError:
            raise SpeechProviderError("the server returned a non-JSON reply") from None
        return parse_verbose_json(data, duration)
    if response_format == "srt":
        return Transcription(parse_srt(text, duration))
    if response_format == "vtt":
        return Transcription(parse_vtt(text, duration))
    return _coarse(text, duration, None)  # "text"


def merge(parts: Iterable[tuple[float, Transcription]]) -> Transcription:
    """Concatenate chunk transcriptions, shifting each by its start offset."""
    segs: list[TranscriptSegment] = []
    lang: str | None = None
    ts = "segment"
    for offset, t in parts:
        segs.extend(s.shifted(offset) for s in t.segments)
        lang = lang or t.language
        if t.timestamps == "coarse":
            ts = "coarse"
    return Transcription(segs, lang, ts)  # type: ignore[arg-type]
