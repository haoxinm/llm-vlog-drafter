"""Captions from transcripts (DESIGN §13.4): an SRT of what is said in the timeline, made only from the transcript's own words.

Rules
  * The words are the transcript's, in the transcript's order, never paraphrased, corrected, translated or completed. The only things done to
    them are splitting a segment into several cues (at word boundaries, by `max_chars_per_line` / `max_lines`) and, for a segment the clip cuts
    through, keeping the words that fall inside the clip's used range.
  * Timing is mapped from source time to record time through the edit plan: record = clip start + (source time - clip source_in). A cue is clipped
    to the clip's used source range, so nothing is shown that the clip does not play.
  * Only clips whose speech is heard (audio_policy natural_sound), at normal speed (the clip's source range and its length on the timeline agree
    to a frame), whose asset has a transcript of its CURRENT content. Everything else is skipped and listed with the reason. A transcript segment
    with coarse timestamps (one block for the whole file, no real timing) is skipped: its time cannot be mapped honestly.
  * Estimates: the transcript has segment timing only, so inside a segment the time of a word is estimated from its position in the text. That is
    used to split a long segment into cues and to cut a segment at a clip boundary. It is stated in the report; it never changes words.
  * The file is UTF-8 without a byte order mark, with CRLF line endings (the SRT convention; Resolve, ffmpeg and common players read it).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.planning.duration import parse_fps
from trip_vlog.planning.models import EditClip, EditPlan
from trip_vlog.qc.checks import TranscriptSpan
from trip_vlog.state import atomic_write_bytes

SRT_NAME = "timeline.srt"
REPORT_NAME = "captions_report.json"
SPEECH_ROLES = ("primary_video", "overlay", "natural_audio")


@dataclass(frozen=True)
class CaptionSettings:
    max_chars_per_line: int = 42
    max_lines: int = 2
    min_duration_seconds: float = 1.0

    @classmethod
    def from_config(cls, config: Any) -> "CaptionSettings":
        c = config.captions
        return cls(c.max_chars_per_line, c.max_lines, float(c.min_duration_seconds))


@dataclass
class Cue:
    start: float                 # seconds on the timeline
    end: float
    lines: tuple[str, ...]
    clip_instance_id: str
    transcript_id: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class CaptionResult:
    cues: list[Cue] = field(default_factory=list)
    clips_captioned: int = 0
    clips_skipped: list[dict[str, str]] = field(default_factory=list)       # {"clip_instance_id", "reason"}
    segments_used: int = 0
    segments_cut_by_clip: int = 0                                            # a clip began or ended inside the segment: words kept by position
    segments_skipped_coarse: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"cues": len(self.cues), "clips_captioned": self.clips_captioned, "clips_skipped": self.clips_skipped, "segments_used": self.segments_used,
                "segments_cut_by_clip": self.segments_cut_by_clip, "segments_skipped_coarse": self.segments_skipped_coarse}


# ---- text ---------------------------------------------------------------------------------------------------------

def wrap_words(words: Sequence[str], max_chars: int) -> list[str]:
    """Greedy line breaks between words. A word longer than the limit stays whole on a line of its own (words are never broken)."""
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _clean(text: str) -> list[str]:
    """Words of a transcript text: whitespace-separated, control characters dropped. Nothing else is touched."""
    return "".join(ch if (ch >= " " or ch in "\t\n") else " " for ch in text).split()


def _word_spans(words: Sequence[str]) -> list[tuple[float, float]]:
    """Each word's [start, end) share of the segment (0..1) by character position, spaces counted between words."""
    total = sum(len(w) for w in words) + max(len(words) - 1, 0)
    out, pos = [], 0
    for w in words:
        out.append((pos / total, (pos + len(w)) / total))
        pos += len(w) + 1
    return out


def _cues_for(words: list[str], t0: float, t1: float, s: CaptionSettings, clip_id: str, tid: str | None) -> list[Cue]:
    """Split `words` (shown from t0 to t1) into cues of at most max_lines lines; each cue's time is its share of the characters."""
    lines = wrap_words(words, s.max_chars_per_line)
    groups = [lines[i:i + s.max_lines] for i in range(0, len(lines), s.max_lines)]
    sizes = [sum(len(x) for x in g) + max(len(g) - 1, 0) for g in groups]
    total = sum(sizes) or 1
    cues, acc = [], 0
    for g, n in zip(groups, sizes):
        a = t0 + (t1 - t0) * acc / total
        acc += n
        b = t0 + (t1 - t0) * acc / total
        cues.append(Cue(a, b, tuple(g), clip_id, tid))
    return cues


# ---- plan + transcripts -> cues -----------------------------------------------------------------------------------

def _skip_reason(c: EditClip, fps: Fraction, have_transcript: bool) -> str | None:
    if c.media_type == "image":
        return "still image"
    if c.track_role not in SPEECH_ROLES:
        return f"track role {c.track_role} carries no speech"
    if c.audio_policy != "natural_sound":
        return f"audio_policy {c.audio_policy}: the speech is not played at full level"
    if c.source_in_seconds is None or c.source_out_seconds is None:
        return "no source range"
    src = Fraction(c.source_out_seconds).limit_denominator(100000) - Fraction(c.source_in_seconds).limit_denominator(100000)
    if abs(src - Fraction(c.duration_frames) / fps) > Fraction(3, 2) / fps:
        return "the source range and the clip length differ (speed changes are not supported for captions)"
    if not have_transcript:
        return "no transcript for this asset"
    return None


def build_captions(plan: EditPlan, transcripts: Mapping[str, Sequence[TranscriptSpan]], settings: CaptionSettings) -> CaptionResult:
    fps = parse_fps(plan.sequence.fps)
    res = CaptionResult()
    cues: list[Cue] = []
    clips = sorted((c for s in plan.scenes for c in s.clips), key=lambda c: (c.timeline_start_frame, c.clip_instance_id))
    for c in clips:
        spans = list(transcripts.get(c.asset_id, ()))
        why = _skip_reason(c, fps, bool(spans))
        if why:
            if c.media_type != "image":
                res.clips_skipped.append({"clip_instance_id": c.clip_instance_id, "reason": why})
            continue
        assert c.source_in_seconds is not None
        s_in = c.source_in_seconds
        s_out = min(c.source_out_seconds or s_in, s_in + float(Fraction(c.duration_frames) / fps))
        rec0 = float(Fraction(c.timeline_start_frame) / fps)
        rec_end = rec0 + (s_out - s_in)
        got = 0
        for sp in sorted(spans, key=lambda x: (x.start, x.end)):
            if sp.coarse:
                res.segments_skipped_coarse += 1
                continue
            if sp.end <= s_in or sp.start >= s_out or sp.end <= sp.start:
                continue
            words = _clean(sp.text)
            if not words:
                continue
            a, b = max(sp.start, s_in), min(sp.end, s_out)
            if a > sp.start or b < sp.end:       # the clip cuts through this segment: keep the words whose estimated middle lies inside the clip
                dur = sp.end - sp.start
                keep = [w for w, (x, y) in zip(words, _word_spans(words)) if s_in <= sp.start + dur * (x + y) / 2 < s_out]
                if not keep:
                    continue
                res.segments_cut_by_clip += 1
                words = keep
            res.segments_used += 1
            got += 1
            cues += _cues_for(words, rec0 + (a - s_in), rec0 + (b - s_in), settings, c.clip_instance_id, sp.transcript_id)
        if got:
            res.clips_captioned += 1
        else:
            res.clips_skipped.append({"clip_instance_id": c.clip_instance_id, "reason": "no usable transcript segment inside the used range"})
        _lengthen_short(cues, c.clip_instance_id, settings.min_duration_seconds, rec_end)
    res.cues = _tidy(cues)
    return res


def _lengthen_short(cues: list[Cue], clip_id: str, minimum: float, clip_end: float) -> None:
    """A cue shown for less than `minimum` stays up longer, up to the start of the next cue or the end of its clip. The text is not touched."""
    mine = [q for q in cues if q.clip_instance_id == clip_id]
    for i, q in enumerate(mine):
        if q.end - q.start < minimum:
            limit = min(mine[i + 1].start if i + 1 < len(mine) else clip_end, clip_end)
            q.end = max(q.end, min(q.start + minimum, limit))


def _tidy(cues: list[Cue]) -> list[Cue]:
    """In time order, no overlap (an end is pulled back to the next start), and nothing shorter than a millisecond."""
    cues = sorted(cues, key=lambda q: (q.start, q.end))
    for a, b in zip(cues, cues[1:]):
        if a.end > b.start:
            a.end = b.start
    return [q for q in cues if q.end - q.start >= 0.001]


# ---- SRT ----------------------------------------------------------------------------------------------------------

def srt_timestamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(cues: Sequence[Cue]) -> bytes:
    """UTF-8, no byte order mark, CRLF line endings, a blank line after every cue."""
    out: list[str] = []
    for i, q in enumerate(cues, 1):
        out += [str(i), f"{srt_timestamp(q.start)} --> {srt_timestamp(q.end)}", *q.lines, ""]
    return "\r\n".join(out).encode("utf-8") + (b"\r\n" if out else b"")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_srt(path: Path, cues: Sequence[Cue], recorded_sha: str | None) -> dict[str, Any]:
    """Write `timeline.srt` atomically. An existing file that is not the one this tool wrote last (`recorded_sha`) was edited by the person: it is kept as
    `timeline.<hash>.bak.srt` before the new one replaces it. Returns {"sha256", "backup"}. With no cues nothing is written and an earlier file is set
    aside as `timeline.stale.srt` (an old caption file must not sit next to a timeline it no longer matches)."""
    backup = None
    if path.is_file():
        cur = sha256_bytes(path.read_bytes())
        if cur != recorded_sha:
            bak = path.with_name(f"{path.stem}.{cur[:12]}.bak{path.suffix}")
            if not bak.exists():
                bak.write_bytes(path.read_bytes())
            backup = bak.name
        if not cues:
            path.replace(path.with_name(f"{path.stem}.stale{path.suffix}"))
    if not cues:
        return {"sha256": None, "backup": backup}
    data = render_srt(cues)
    atomic_write_bytes(path, data)
    return {"sha256": sha256_bytes(data), "backup": backup}
