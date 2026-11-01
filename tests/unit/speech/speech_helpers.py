"""Doubles for the transcription stage tests: a backend double, a stub for ffmpeg audio extraction and synthetic A/V assets."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from analysis_helpers import add_video, make_ctx

from trip_vlog.config import parse_config
from trip_vlog.media.segment import SegmentSettings, build_segments, moments_from_segments
from trip_vlog.speech import audio as au
from trip_vlog.speech.types import Transcription, TranscriptSegment

LLM = {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}}


def speech_cfg(backend: str = "openai_transcriptions", *, speech: dict | None = None, media: dict | None = None, project: dict | None = None):
    sp: dict[str, Any] = {"backend": backend, "endpoint": {"base_url": "http://127.0.0.1:8000", "api_key_env": None, "model": "test-model"},
                          "command": {"executable": "whisper-cli", "args": ["-f", "{input}", "-of", "{output_base}"]}}
    sp.update(speech or {})
    return parse_config({"llm": LLM, "speech": sp, "media": media or {}, "project": project or {}})


class FakeBackend:
    """Returns the scripted Transcription for each call (default: one segment 'hello' 0.5-1.5 s)."""

    def __init__(self, name: str = "openai_transcriptions", model: str = "test-model", script=None):
        self.name, self.model, self.calls, self.script = name, model, [], script

    def transcribe(self, path: Path, duration):
        self.calls.append((path.name, duration, path.is_file(), path.read_bytes() if path.is_file() else None))
        if self.script is not None:
            out = self.script(len(self.calls), path, duration)
            if isinstance(out, BaseException):
                raise out
            if out is not None:
                return out
        return Transcription([TranscriptSegment(0.5, 1.5, f"hello {len(self.calls)}", 0.9, -0.1, 0.01)], "en", "segment")


class Extractor:
    """Stands in for ffmpeg audio extraction: writes a small file and records the requested range."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def __call__(self, ffmpeg, src, dest, fmt="wav", *, start=None, duration=None, timeout=0):
        self.calls.append({"src": Path(src).name, "dest": Path(dest), "fmt": fmt, "start": start, "duration": duration})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"{fmt}:{start}:{duration}".encode())
        return None


def install(monkeypatch) -> Extractor:
    ex = Extractor()
    monkeypatch.setattr(au, "extract_audio", ex)
    return ex


def make_speech_ctx(tmp_path: Path, cfg=None):
    return make_ctx(tmp_path, cfg or speech_cfg(), ffmpeg=Path("ffmpeg"))


def add_av(ctx: SimpleNamespace, rel: str, duration: float, *, audio: bool = True, silent: bool = False, window: float = 6.0,
           cv: str | None = None):
    """A video with (or without) an audio stream; every segment is flagged silent or not; the media file exists."""
    aid, _ = add_video(ctx.catalog, rel, duration, cv=cv, window=window)
    row = ctx.catalog.get_asset(aid)
    cv = row["content_version"]
    ctx.catalog.conn.execute("UPDATE assets SET audio_streams=? WHERE asset_id=?", (1 if audio else 0, aid))
    ctx.catalog.conn.commit()
    n = int(duration * 4)
    det = {"cuts": [], "silences": [], "has_audio": audio,
           "windows": [[0.25 * i, -90.0 if silent else -20.0, -80.0 if silent else -10.0] for i in range(n)] if audio else []}
    segs = build_segments(aid, "video", duration, None, det, SegmentSettings(window_seconds=window))
    ctx.catalog.replace_segments(aid, cv, segs)
    ctx.catalog.replace_moments(aid, cv, moments_from_segments(segs, cv))
    path = ctx.paths.media_root / Path(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    return aid, segs


def moments(ctx) -> dict[str, dict]:
    return {r["moment_id"]: json.loads(r["data_json"]) for r in ctx.catalog.list_moments()}


def tr(*segs: tuple[float, float, str], lang: str | None = "en", ts: str = "segment") -> Transcription:
    return Transcription([TranscriptSegment(a, b, t) for a, b, t in segs], lang, ts)  # type: ignore[arg-type]
