"""Music ducking (DESIGN §13.3, §20 Phase 7): where the music is lowered, and the check that it really is in the rendered preview.

Windows. Natural sound the plan keeps (a primary video clip whose audio_policy is not ``muted``, or a stand-alone natural-audio clip)
is where music is lowered. Which part of such a clip counts depends on ``filmcraft.duck_mode``:

  * ``speech``         the transcript segments (non-coarse) that fall in the clip's used source range, padded; a clip whose transcript has
                       no segment timing (coarse) is treated as speech for its whole length (the timing is unknown, never guessed);
  * ``natural_sound``  the same, and in addition the whole clip when its measured audio is active (``audio_active_ratio_min``) and the
                       transcript has no timed segment there.

Windows closer than ``duck_merge_gap_seconds`` are joined (no pumping between words). Pure functions over plain data.

Envelope. Music clip keyframes on FilmCraft's Volume level (dB, added to the clip gain): 0 dB before a window, ``duck_db`` inside it, with
linear ramps of ``duck_ramp_seconds`` in front of and behind it, clamped to the music clip. Frames are whole sequence frames.

Check. ``check_music_ducking`` takes a WAV of the music bed alone (rendered by FilmCraft with the other audio tracks muted in a throw-away
session) and compares, for each duck window, the level of the rendered bed with the level of the music SOURCE over the same source range:
the difference must be the clip gain plus the duck depth (inside a window) or the clip gain (outside). Comparing with the source keeps the
check independent of how loud or dynamic the music is.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.timeline_spec import GainKey
from trip_vlog.media.tools import popen_flags
from trip_vlog.planning.models import EditPlan
from trip_vlog.qc.checks import PASS, SKIPPED, WARN, CheckResult, TranscriptSpan, _audible, _audible_video

PAD_SECONDS = 0.15
TOLERANCE_DB = 2.0          # rendered difference vs. expected, dB (mean level of a window; resampling and mixing add a little)
MIN_MEASURE_SECONDS = 0.5   # a window shorter than this (after the ramps) is not measured


@dataclass(frozen=True)
class DuckSettings:
    enabled: bool = False
    depth_db: float = -12.0
    ramp_seconds: float = 0.3
    merge_gap_seconds: float = 1.0
    mode: str = "speech"             # speech | natural_sound
    active_ratio_min: float = 0.5

    @classmethod
    def from_config(cls, config: Any) -> "DuckSettings":
        f = config.filmcraft
        return cls(bool(f.duck_music), float(f.duck_db), float(f.duck_ramp_seconds), float(f.duck_merge_gap_seconds), f.duck_mode,
                   float(config.qc.audio_active_ratio_min))


@dataclass(frozen=True)
class DuckWindow:
    start_frame: int
    end_frame: int           # exclusive
    reason: str              # speech | speech_untimed | active_sound | natural_audio
    clip_instance_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"start_frame": self.start_frame, "end_frame": self.end_frame, "reason": self.reason, "clips": list(self.clip_instance_ids)}


@dataclass
class DuckPlan:
    windows: list[DuckWindow] = field(default_factory=list)
    keys: dict[str, tuple[GainKey, ...]] = field(default_factory=dict)       # music clip_instance_id -> keyframes
    notes: list[str] = field(default_factory=list)
    settings: DuckSettings = DuckSettings()

    def to_dict(self) -> dict[str, Any]:
        return {"depth_db": self.settings.depth_db, "ramp_seconds": self.settings.ramp_seconds, "mode": self.settings.mode,
                "windows": [w.to_dict() for w in self.windows], "notes": self.notes,
                "music_clips": {k: [g.to_dict() for g in v] for k, v in self.keys.items()}}


def _clip_windows(plan: EditPlan, clip_audio: Mapping[str, Mapping[str, Any]], transcripts: Mapping[str, Sequence[TranscriptSpan]],
                  s: DuckSettings) -> list[DuckWindow]:
    fps = Fraction(plan.sequence.fps)
    pad = int(round(PAD_SECONDS * float(fps)))
    out: list[DuckWindow] = []
    for sc in plan.scenes:
        for c in sc.clips:
            lo, hi = c.timeline_start_frame, c.timeline_start_frame + c.duration_frames
            if c.media_type == "audio" and c.track_role == "natural_audio" and c.audio_policy != "muted":
                if s.mode == "natural_sound":
                    out.append(DuckWindow(lo, hi, "natural_audio", (c.clip_instance_id,)))
                continue
            if c.track_role != "primary_video" or not _audible_video(c):
                continue
            segs = [t for t in transcripts.get(c.asset_id, ()) if t.text.strip()]
            timed = [t for t in segs if not t.coarse]
            s_in, s_out = float(c.source_in_seconds), float(c.source_out_seconds)
            found = False
            for t in timed:
                a, b = max(t.start, s_in), min(t.end, s_out)
                if b - a <= 0.05:
                    continue
                fa = lo + int(round((a - s_in) * float(fps))) - pad
                fb = lo + int(round((b - s_in) * float(fps))) + pad
                out.append(DuckWindow(max(lo, fa), min(hi, fb), "speech", (c.clip_instance_id,)))
                found = True
            if found:
                continue
            if segs and not timed:
                out.append(DuckWindow(lo, hi, "speech_untimed", (c.clip_instance_id,)))
            elif s.mode == "natural_sound" and _audible(clip_audio.get(c.clip_instance_id), s.active_ratio_min):
                out.append(DuckWindow(lo, hi, "active_sound", (c.clip_instance_id,)))
    return out


def merge_windows(windows: Sequence[DuckWindow], min_gap_frames: int) -> list[DuckWindow]:
    out: list[DuckWindow] = []
    for w in sorted(windows, key=lambda w: (w.start_frame, w.end_frame)):
        if out and w.start_frame - out[-1].end_frame < min_gap_frames:
            p = out[-1]
            reason = p.reason if p.reason == w.reason else "+".join(sorted({*p.reason.split("+"), *w.reason.split("+")}))
            out[-1] = DuckWindow(p.start_frame, max(p.end_frame, w.end_frame), reason, tuple(dict.fromkeys(p.clip_instance_ids + w.clip_instance_ids)))
        else:
            out.append(w)
    return out


def envelope(windows: Sequence[DuckWindow], clip_start: int, clip_end: int, depth_db: float, ramp: int) -> tuple[GainKey, ...]:
    """Keyframes (absolute Volume level) for the part of ``windows`` inside [clip_start, clip_end). Empty when no window touches the clip."""
    last = clip_end - 1
    raw: list[tuple[int, float]] = []
    for w in windows:
        a, b = max(w.start_frame, clip_start), min(w.end_frame, clip_end)
        if b - a < 1:
            continue
        if a - ramp > clip_start:
            raw.append((a - ramp, 0.0))
        elif a > clip_start:
            raw.append((clip_start, 0.0))
        raw.append((a, depth_db))
        raw.append((min(b, last), depth_db))
        if b + ramp <= last:
            raw.append((b + ramp, 0.0))
        elif b < last:
            raw.append((last, 0.0))
    keys: list[GainKey] = []
    for frame, db in raw:
        if keys and frame <= keys[-1].frame:
            if frame == keys[-1].frame and db < keys[-1].db:
                keys[-1] = GainKey(frame, db)      # the duck wins over a ramp that lands on the same frame
            continue
        keys.append(GainKey(frame, db))
    return tuple(keys)


def build_duck_plan(plan: EditPlan, clip_audio: Mapping[str, Mapping[str, Any]], transcripts: Mapping[str, Sequence[TranscriptSpan]],
                    settings: DuckSettings) -> DuckPlan:
    res = DuckPlan(settings=settings)
    if not settings.enabled:
        return res
    music = [c for sc in plan.scenes for c in sc.clips if c.track_role == "music" and c.media_type == "audio" and c.audio_policy != "muted"]
    if not music:
        res.notes.append("the plan has no music clip; nothing to duck")
        return res
    if not plan.audio.duck_music_under_speech:
        res.notes.append("the plan's audio.duck_music_under_speech is false; ducking was not applied")
        return res
    fps = float(Fraction(plan.sequence.fps))
    ramp = max(1, int(round(settings.ramp_seconds * fps)))
    gap = max(int(round(settings.merge_gap_seconds * fps)), 2 * ramp + 2)
    res.windows = merge_windows(_clip_windows(plan, clip_audio, transcripts, settings), gap)
    if not res.windows:
        res.notes.append("no speech or active natural sound found under the music; no ducking keyframes written")
    for m in music:
        keys = envelope(res.windows, m.timeline_start_frame, m.timeline_start_frame + m.duration_frames, settings.depth_db, ramp)
        if keys:
            res.keys[m.clip_instance_id] = keys
    return res


# ---- measuring the rendered music bed ------------------------------------------------------------------------

_MEAN = re.compile(r"mean_volume:\s*(-?[0-9.]+|-inf)\s*dB")


def mean_level_db(ffmpeg: Path | str, media: Path | str, start: float, duration: float) -> float | None:
    """Mean level (dB) of ``duration`` seconds of ``media`` from ``start``, via ffmpeg volumedetect. None when it cannot be measured."""
    try:
        cp = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                             "-i", str(media), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, **popen_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    m = _MEAN.search(cp.stderr or "")
    if not m or m[1] == "-inf":
        return None
    return float(m[1])


def check_music_ducking(plan: EditPlan, duck: DuckPlan, bed: Path | None, source_paths: Mapping[str, str], ffmpeg: Path | str | None,
                        *, music_gain_db: Mapping[str, float] | None = None, bed_error: str | None = None,
                        tolerance_db: float = TOLERANCE_DB) -> CheckResult:
    """Pass when the music bed in the preview sits ``duck depth`` below the source inside the windows and at its clip gain outside them."""
    name = "music_ducking"
    if not duck.settings.enabled:
        return CheckResult(name, SKIPPED, "skipped: music ducking is off (filmcraft.duck_music)", {"skipped": True, "reason": "off"})
    if not duck.keys:
        return CheckResult(name, SKIPPED, "skipped: " + (duck.notes[0] if duck.notes else "no ducking keyframes were written"),
                           {"skipped": True, "reason": "no_keys"})
    if bed_error or bed is None or not Path(bed).is_file():
        return CheckResult(name, WARN, f"the music bed could not be rendered for measuring: {bed_error or 'no file'}", {"bed_error": bed_error})
    if ffmpeg is None:
        return CheckResult(name, SKIPPED, "skipped: ffmpeg was not found, so the preview cannot be measured", {"skipped": True, "reason": "no_ffmpeg"})
    fps = float(Fraction(plan.sequence.fps))
    ramp = max(1, int(round(duck.settings.ramp_seconds * fps)))
    gains = music_gain_db or {}
    findings: list[dict[str, Any]] = []
    measured: list[dict[str, Any]] = []
    clips = {c.clip_instance_id: c for sc in plan.scenes for c in sc.clips}
    for cid, keys in duck.keys.items():
        c = clips[cid]
        src = source_paths.get(cid)
        if not src:
            continue
        lo, hi = c.timeline_start_frame, c.timeline_start_frame + c.duration_frames
        spans: list[tuple[int, int, float, str]] = []     # (start frame, end frame, expected delta dB, kind)
        ducked = [(w.start_frame, w.end_frame) for w in duck.windows if w.end_frame > lo and w.start_frame < hi]
        for a, b in ducked:
            spans.append((max(a, lo) + ramp, min(b, hi) - ramp, duck.settings.depth_db, "ducked"))
        prev = lo
        for a, b in ducked:
            if a - ramp - prev >= 1:
                spans.append((prev + ramp // 2 + 1, a - ramp - 1, 0.0, "open"))
            prev = b
        if hi - (prev + ramp) >= 1:
            spans.append((prev + ramp + 1, hi - 1, 0.0, "open"))
        for a, b, delta, kind in spans:
            if (b - a) / fps < MIN_MEASURE_SECONDS:
                continue
            got = mean_level_db(ffmpeg, bed, a / fps, (b - a) / fps)
            s_in = float(c.source_in_seconds) + (a - lo) / fps
            ref = mean_level_db(ffmpeg, src, s_in, (b - a) / fps)
            if got is None or ref is None:
                continue
            expected = gains.get(cid, 0.0) + delta
            diff = got - ref
            row = {"clip_instance_id": cid, "kind": kind, "start_frame": a, "end_frame": b, "rendered_minus_source_db": round(diff, 1),
                   "expected_db": round(expected, 1), "ok": abs(diff - expected) <= tolerance_db}
            measured.append(row)
            if not row["ok"]:
                findings.append(row)
    if not measured:
        return CheckResult(name, SKIPPED, "skipped: no window was long and audible enough to measure", {"skipped": True, "reason": "nothing_measurable"})
    ducked_rows = [m for m in measured if m["kind"] == "ducked"]
    if findings:
        f = findings[0]
        return CheckResult(name, WARN, f"{len(findings)} of {len(measured)} measured span(s) of the music bed differ from the plan by more than "
                           f"{tolerance_db:g} dB; e.g. {f['clip_instance_id']} frames {f['start_frame']}-{f['end_frame']} ({f['kind']}): rendered "
                           f"{f['rendered_minus_source_db']:g} dB against the source, expected {f['expected_db']:g} dB",
                           {"findings": findings[:30], "measured": measured[:60], "total": len(findings)})
    return CheckResult(name, PASS, f"the music bed sits {abs(duck.settings.depth_db):g} dB lower inside {len(ducked_rows)} measured duck window(s) and "
                       f"at its clip gain outside ({len(measured)} span(s), tolerance {tolerance_db:g} dB)", {"measured": measured[:60]})
