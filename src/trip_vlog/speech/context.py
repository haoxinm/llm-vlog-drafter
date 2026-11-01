"""Transcripts as data: stored rows, overlap with moments/segments, and the verbatim text shown to the vision model."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from trip_vlog.speech.types import TranscriptSegment


def transcript_id(asset_id: str, index: int, start: float, end: float) -> str:
    return "tr_" + hashlib.sha256(f"{asset_id}:{index}:{start:.3f}:{end:.3f}".encode("utf-8")).hexdigest()[:16]


def to_records(asset_id: str, content_version: str | None, segments: Sequence[TranscriptSegment], *, language: str | None,
               timestamps: str, backend: str, model: str | None) -> list[dict[str, Any]]:
    """Catalog/JSONL records. `confidence` is exp(avg_logprob) when reported, else null; text is verbatim (whitespace-normalised)."""
    out = []
    for i, s in enumerate(segments):
        out.append({"transcript_id": transcript_id(asset_id, i, s.start, s.end), "asset_id": asset_id, "index": i,
                    "start_seconds": s.start, "end_seconds": s.end, "text": s.text, "confidence": s.confidence,
                    "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob, "language": language,
                    "timestamps": timestamps, "backend": backend, "model": model, "content_version": content_version,
                    "kind": "verbatim"})  # spoken words as heard, never an inferred summary (DESIGN §7.3)
    return out


def rows_for(cat: Any, asset_id: str, content_version: str | None) -> list[dict[str, Any]]:
    return [json.loads(r["data_json"]) for r in cat.list_transcripts(asset_id) if r["content_version"] == content_version]


def overlaps(t_start: float, t_end: float, start: float, end: float) -> bool:
    if t_end <= t_start:  # zero-length cue: inside the range
        return start <= t_start < end
    return t_start < end and t_end > start


def usable_rows(rows: Sequence[dict[str, Any]], n_segments: int | None) -> Sequence[dict[str, Any]]:
    """Rows that may be attached to a segment of an asset cut into `n_segments`. A coarse transcript (plain text, one row spanning
    the whole clip) says nothing about WHERE the words were spoken, so it is only attached when the asset is a single segment."""
    if n_segments is not None and n_segments > 1 and any(r.get("timestamps") == "coarse" for r in rows):
        return []
    return rows


def refs_for(rows: Sequence[dict[str, Any]], start: float | None, end: float | None, n_segments: int | None = None) -> list[str]:
    """Transcript ids whose time range overlaps [start, end). Stills (no range) have none; coarse transcripts only attach to
    single-segment assets (`n_segments`)."""
    if start is None or end is None:
        return []
    rows = usable_rows(rows, n_segments)
    return [r["transcript_id"] for r in rows if overlaps(r["start_seconds"], r["end_seconds"], start, end)]


def text_for(rows: Sequence[dict[str, Any]], start: float | None, end: float | None, n_segments: int | None = None) -> str:
    """Verbatim text of the transcript segments overlapping [start, end), joined by spaces ('' when none; coarse transcripts
    only for single-segment assets)."""
    if start is None or end is None:
        return ""
    rows = usable_rows(rows, n_segments)
    return " ".join(r["text"] for r in rows if overlaps(r["start_seconds"], r["end_seconds"], start, end)).strip()


def text_digest(text: str, max_chars: int) -> str:
    """Identity of the text as the model will see it (cut at max_chars, plus whether it was cut)."""
    shown = text[:max_chars]
    return hashlib.sha256(f"{max_chars}:{int(len(text) > max_chars)}:{shown}".encode("utf-8")).hexdigest()[:16]
