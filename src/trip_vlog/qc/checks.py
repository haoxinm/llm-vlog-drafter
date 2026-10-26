"""Deterministic QC checks (DESIGN §14.1). Each check is a pure function over plain data and returns a CheckResult."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.inspect import ProjectSnapshot, SequenceSnapshot
from trip_vlog.media.tools import popen_flags
from trip_vlog.planning.catalog_view import PlanningCatalog
from trip_vlog.planning.duration import parse_fps
from trip_vlog.planning.models import EditPlan

PASS, WARN, FAIL, SKIPPED = "pass", "warn", "fail", "skipped"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # pass | warn | fail | skipped (the check could not run; counts like a pass for the overall status)
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.data:
            d["data"] = dict(self.data)
        return d


def overall_status(results: Sequence[CheckResult]) -> str:
    if any(r.status == FAIL for r in results):
        return FAIL
    return WARN if any(r.status == WARN for r in results) else PASS   # skipped checks neither warn nor fail


def check_media_online(project: ProjectSnapshot | None, source_paths: Sequence[str]) -> CheckResult:
    missing = [p for p in source_paths if not Path(p).is_file()]
    offline = [m.path for m in (project.media if project else ()) if m.status != "online"]
    if missing or offline:
        return CheckResult("media_online", FAIL, f"{len(missing)} source file(s) missing, {len(offline)} offline in FilmCraft",
                           {"missing": missing[:20], "offline": offline[:20]})
    n = len(project.media) if project else 0
    return CheckResult("media_online", PASS, f"{len(source_paths)} source file(s) exist; {n} FilmCraft media item(s) online")


def check_project_reopens(reopened: bool, mismatches: Sequence[str], error: str | None = None) -> CheckResult:
    if not reopened:
        return CheckResult("project_reopens", FAIL, f"project could not be reopened in a new FilmCraft process: {error}")
    if mismatches:
        return CheckResult("project_reopens", FAIL, f"reopened project differs from the plan ({len(mismatches)} mismatch(es))",
                           {"mismatches": list(mismatches)[:20]})
    return CheckResult("project_reopens", PASS, "project reopened in a fresh FilmCraft process and matches the plan")


def check_v1_gaps(seq: SequenceSnapshot) -> CheckResult:
    v1 = seq.track("V1")
    clips = sorted(v1.clips, key=lambda c: c.start_frame) if v1 else []
    if not clips:
        return CheckResult("no_v1_gaps", FAIL, "V1 has no clips")
    gaps: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    if clips[0].start_frame > 0:
        gaps.append({"at_frame": 0, "frames": clips[0].start_frame})
    for a, b in zip(clips, clips[1:]):
        if b.start_frame > a.end_frame:
            gaps.append({"at_frame": a.end_frame, "frames": b.start_frame - a.end_frame})
        elif b.start_frame < a.end_frame:
            overlaps.append({"at_frame": b.start_frame, "frames": a.end_frame - b.start_frame})
    if gaps:
        return CheckResult("no_v1_gaps", FAIL, f"{len(gaps)} gap(s) between V1 clips", {"gaps": gaps[:20]})
    if overlaps:
        return CheckResult("no_v1_gaps", WARN, f"{len(overlaps)} overlap(s) on V1", {"overlaps": overlaps[:20]})
    return CheckResult("no_v1_gaps", PASS, f"{len(clips)} V1 clips are contiguous")


def check_duplicates(plan: EditPlan, catalog: PlanningCatalog) -> CheckResult:
    clips = [c for s in plan.scenes for c in s.clips]
    found: list[str] = []
    seen_moments: dict[str, str] = {}
    for c in clips:
        if c.moment_id in seen_moments and not c.intentional_repeat_reason:
            found.append(f"{c.clip_instance_id} repeats moment {c.moment_id} (also {seen_moments[c.moment_id]})")
        seen_moments.setdefault(c.moment_id, c.clip_instance_id)
    for i, x in enumerate(clips):
        for y in clips[i + 1:]:
            if x.asset_id != y.asset_id or x.moment_id == y.moment_id or x.media_type != "video" or y.media_type != "video":
                continue
            if None in (x.source_in_frame, x.source_out_frame, y.source_in_frame, y.source_out_frame):
                continue
            if min(x.source_out_frame, y.source_out_frame) > max(x.source_in_frame, y.source_in_frame) and not (  # type: ignore[type-var]
                    x.intentional_repeat_reason or y.intentional_repeat_reason):
                found.append(f"{x.clip_instance_id} and {y.clip_instance_id} overlap in the source")
    by_version: dict[str, list[str]] = {}
    for aid in dict.fromkeys(c.asset_id for c in clips):
        a = catalog.assets.get(aid)
        if a and a.content_version:
            by_version.setdefault(a.content_version, []).append(a.relative_path)
    for paths in by_version.values():
        if len(paths) > 1:
            found.append("identical content used from different files: " + ", ".join(paths))
    if found:
        return CheckResult("duplicate_scenes", WARN, f"{len(found)} possible duplicate(s)", {"findings": found[:20]})
    return CheckResult("duplicate_scenes", PASS, "no duplicate scenes detected")


def check_stills(plan: EditPlan, min_seconds: float) -> CheckResult:
    fps = parse_fps(plan.sequence.fps)
    stills = [c for s in plan.scenes for c in s.clips if c.media_type == "image"]
    short = [c.clip_instance_id for c in stills if Fraction(c.duration_frames) / fps < Fraction(str(min_seconds))]
    if short:
        return CheckResult("still_durations", FAIL, f"{len(short)} still(s) shorter than {min_seconds:g}s", {"clips": short})
    return CheckResult("still_durations", PASS, f"{len(stills)} still(s), all at least {min_seconds:g}s")


def check_total_duration(seq: SequenceSnapshot, target: float, pct: float, seconds: float) -> CheckResult:
    actual = float(Fraction(seq.duration_frames) / seq.fps)
    tol = max(target * pct / 100.0, seconds)
    data = {"actual_seconds": round(actual, 3), "target_seconds": target, "tolerance_seconds": round(tol, 3)}
    if abs(actual - target) > tol:
        return CheckResult("total_duration", FAIL, f"{actual:.2f}s vs target {target:g}s (tolerance {tol:.2f}s)", data)
    return CheckResult("total_duration", PASS, f"{actual:.2f}s vs target {target:g}s (tolerance {tol:.2f}s)", data)


def check_preview(probe: Mapping[str, Any] | None, *, width: int, height: int, fps: Fraction, frames: int,
                  expect_audio: bool, scale: float, error: str | None = None) -> CheckResult:
    from trip_vlog.filmcraft.preview import evaluate_preview

    if probe is None:
        return CheckResult("preview_playable", FAIL, error or "no preview file")
    rep = evaluate_preview(probe, path="preview", width=width, height=height, fps=fps, frames=frames,
                           expect_audio=expect_audio, scale=scale)
    data = {"duration_seconds": rep.duration_seconds, "width": rep.width, "height": rep.height, "fps": rep.fps,
            "video_codec": rep.video_codec, "audio_streams": rep.audio_streams}
    if not rep.ok:
        return CheckResult("preview_playable", FAIL, "; ".join(f"{c.name}: {c.detail}" for c in rep.failures), data)
    return CheckResult("preview_playable", PASS, f"{rep.width}x{rep.height} @ {rep.fps}, {rep.duration_seconds:.2f}s, "
                       f"{rep.audio_streams} audio stream(s)", data)


_BLACK = re.compile(r"black_start:(?P<s>[0-9.]+)\s+black_end:(?P<e>[0-9.]+)\s+black_duration:(?P<d>[0-9.]+)")


def parse_blackdetect(text: str) -> list[dict[str, float]]:
    return [{"start": float(m["s"]), "end": float(m["e"]), "duration": float(m["d"])} for m in _BLACK.finditer(text)]


def detect_black(ffmpeg: Path, preview: Path, min_seconds: float, pic_th: float, pix_th: float,
                 timeout: float = 600.0) -> list[dict[str, float]]:
    cp = subprocess.run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(preview), "-vf",
                         f"blackdetect=d={min_seconds}:pic_th={pic_th}:pix_th={pix_th}", "-an", "-f", "null", "-"],
                        capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                        shell=False, **popen_flags())
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg blackdetect failed: {cp.stderr.strip()[-300:]}")
    return parse_blackdetect(cp.stderr)


def check_black_frames(intervals: list[dict[str, float]] | None, error: str | None = None) -> CheckResult:
    if intervals is None:
        return CheckResult("black_frames", WARN, error or "black-frame detection was not run")
    if intervals:
        total = sum(i["duration"] for i in intervals)
        return CheckResult("black_frames", WARN, f"{len(intervals)} black interval(s), {total:.2f}s in total; review them",
                           {"intervals": intervals[:20]})
    return CheckResult("black_frames", PASS, "no black intervals detected")


def check_titles(plan: EditPlan, seq: SequenceSnapshot | None) -> CheckResult | None:
    """Titles of the plan against the V2 clips read back from the reopened project (None when the plan has no titles)."""
    if not plan.titles:
        return None
    if seq is None:
        return CheckResult("titles", WARN, f"{len(plan.titles)} title(s) in the plan could not be read back (project did not reopen)")
    v2 = seq.track("V2")
    clips = list(v2.clips) if v2 else []
    missing = []
    for t in plan.titles:
        hit = next((c for c in clips if c.name == t.text and abs(c.start_frame - t.start_frame) <= 1), None)
        if hit is None:
            missing.append(t.title_id)
    if missing:
        return CheckResult("titles", FAIL, f"{len(missing)} of {len(plan.titles)} title(s) not found on V2 at the planned frame", {"titles": missing})
    return CheckResult("titles", PASS, f"{len(plan.titles)} title(s) on V2 read back at the planned frames; interchange files do not carry them "
                       "(recreate with `trip-vlog resolve-finish` or from the handoff report)")


def check_orientation(plan: EditPlan, catalog: PlanningCatalog) -> CheckResult:
    seq_o = "portrait" if plan.sequence.height > plan.sequence.width else "landscape" if plan.sequence.width > plan.sequence.height else "square"
    off = []
    for s in plan.scenes:
        for c in s.clips:
            a = catalog.assets.get(c.asset_id)
            if a and c.media_type in ("video", "image") and a.orientation in ("portrait", "landscape") and seq_o != "square" and a.orientation != seq_o:
                off.append(f"{c.clip_instance_id} ({a.relative_path}) is {a.orientation}")
    if off:
        return CheckResult("orientation", WARN, f"{len(off)} {('portrait' if seq_o == 'landscape' else 'landscape')} clip(s) in a {seq_o} sequence: "
                           "manual framing review needed", {"clips": off[:20]})
    return CheckResult("orientation", PASS, f"all visual clips match the {seq_o} sequence")


# ---- decode ------------------------------------------------------------------------------------------------

def evaluate_decode(returncode: int, stderr: str, error: str | None = None) -> CheckResult:
    """Pure part of `check_preview_decodes`: a non-zero exit is an objective defect; diagnostics on a clean exit are a warning."""
    if error:
        return CheckResult("preview_decodes", WARN, f"decode test was not run: {error}")
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if returncode != 0:
        return CheckResult("preview_decodes", FAIL, f"ffmpeg could not decode the preview to the end (exit {returncode})", {"stderr": lines[-5:]})
    if lines:
        return CheckResult("preview_decodes", WARN, f"the preview decodes, with {len(lines)} decoder message(s)", {"stderr": lines[:5]})
    return CheckResult("preview_decodes", PASS, "the whole preview (video and audio) decodes without errors")


def check_preview_decodes(ffmpeg: Path | None, preview: Path, timeout: float = 600.0) -> CheckResult:
    """Decode every video and audio frame of the preview to nowhere: 'output is playable' beyond what ffprobe reads from headers."""
    if ffmpeg is None:
        return evaluate_decode(0, "", "ffmpeg not found")
    try:
        cp = subprocess.run([str(ffmpeg), "-hide_banner", "-nostats", "-v", "error", "-i", str(preview), "-f", "null", "-"],
                            capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                            shell=False, **popen_flags())
    except (OSError, subprocess.SubprocessError) as e:
        return evaluate_decode(0, "", f"{type(e).__name__}: {e}")
    return evaluate_decode(cp.returncode, cp.stderr)


# ---- speech, audio and music (DESIGN 14.1): deterministic heuristics, they warn and never fail ---------------

@dataclass(frozen=True)
class TranscriptSpan:
    """One transcript segment of a source asset (seconds in the SOURCE file). ``coarse``: the backend gave no real timing."""
    start: float
    end: float
    text: str
    coarse: bool = False
    transcript_id: str | None = None


def _skipped(name: str, reason: str) -> CheckResult:
    return CheckResult(name, SKIPPED, f"skipped: {reason}", {"skipped": True, "reason": reason})


def _primary_clips(plan: EditPlan) -> list[tuple[str, Any]]:
    """(scene_id, clip) of the primary video track in timeline order."""
    out = [(s.scene_id, c) for s in plan.scenes for c in s.clips if c.track_role == "primary_video"]
    return sorted(out, key=lambda sc: (sc[1].timeline_start_frame, sc[1].clip_instance_id))


def _audible_video(c: Any) -> bool:
    return c.media_type == "video" and c.audio_policy != "muted" and c.source_in_seconds is not None and c.source_out_seconds is not None


def _continues(a: Any, b: Any, fps: Fraction) -> bool:
    """b plays on from where a stopped in the same source file, so the cut between them is not a break in the sound."""
    if a.asset_id != b.asset_id or None in (a.source_out_seconds, b.source_in_seconds):
        return False
    return abs(a.source_out_seconds - b.source_in_seconds) <= float(1 / fps) * 1.01


def _clip_ref(scene_id: str, c: Any) -> dict[str, str]:
    return {"clip_instance_id": c.clip_instance_id, "scene_id": scene_id}


def _words_cut(text: str, start: float, end: float, cut_at: float, side: str, limit: int = 8) -> str:
    """The words of a segment lost at a cut, ESTIMATED by spreading the words evenly over the segment (transcripts carry
    segment timing, not word timing). side 'in': spoken before the clip starts; 'out': spoken after it ends."""
    words = text.split()
    if not words:
        return ""
    frac = (cut_at - start) / (end - start) if end > start else 0.5
    n = min(max(round(frac * len(words)), 1), max(len(words) - 1, 1))
    lost = words[:n] if side == "in" else words[n:]
    if not lost:
        lost = words
    shown = lost[-limit:] if side == "in" else lost[:limit]
    return ("... " if side == "in" and len(lost) > limit else "") + " ".join(shown) + (" ..." if side == "out" and len(lost) > limit else "")


def check_speech_truncation(plan: EditPlan, transcripts: Mapping[str, Sequence[TranscriptSpan]] | None,
                            tolerance_seconds: float = 0.25) -> CheckResult:
    """Warn when a clip's source in/out point falls inside a transcript segment of its asset (by more than the tolerance on
    both sides of the edge), i.e. the cut talks over somebody mid-sentence. Muted clips and edges that continue the same
    source take are not cuts through speech. Skipped when no transcript exists for any used asset."""
    name = "speech_truncation"
    fps = Fraction(plan.sequence.fps)
    used = {c.asset_id for _, c in _primary_clips(plan) if _audible_video(c)}
    spans = {a: [s for s in (transcripts or {}).get(a, ()) if s.text.strip() and not s.coarse] for a in used}
    if not any((transcripts or {}).get(a) for a in used):
        return _skipped(name, "no transcript for the clips in the plan (transcription did not run or found no speech)")
    if not any(spans.values()):
        return _skipped(name, "the transcripts have no segment timing (coarse timestamps), so cuts cannot be compared with speech")
    ordered = _primary_clips(plan)
    found: list[dict[str, Any]] = []
    tol = float(tolerance_seconds)
    for i, (scene_id, c) in enumerate(ordered):
        if not _audible_video(c):
            continue
        prev = ordered[i - 1][1] if i > 0 else None
        nxt = ordered[i + 1][1] if i + 1 < len(ordered) else None
        for side, at, neighbour in (("in", c.source_in_seconds, prev), ("out", c.source_out_seconds, nxt)):
            if neighbour is not None and (_continues(neighbour, c, fps) if side == "in" else _continues(c, neighbour, fps)):
                continue
            for seg in spans.get(c.asset_id, ()):
                if seg.start < at - tol and seg.end > at + tol:
                    found.append({**_clip_ref(scene_id, c), "asset_id": c.asset_id, "edge": side, "source_seconds": round(at, 3),
                                  "segment_start": round(seg.start, 3), "segment_end": round(seg.end, 3), "segment_text": seg.text.strip(),
                                  "transcript_id": seg.transcript_id, "words_cut": _words_cut(seg.text, seg.start, seg.end, at, side),
                                  "words_cut_estimated": True})
    if not found:
        return CheckResult(name, PASS, "no clip edge falls inside a transcribed sentence")
    first = found[0]
    ex = f"{first['clip_instance_id']} {'starts after' if first['edge'] == 'in' else 'ends before'} \"{first['words_cut']}\""
    return CheckResult(name, WARN, f"{len(found)} clip edge(s) cut through speech; e.g. {ex}", {"findings": found[:30], "total": len(found)})


def clip_audio_features(plan: EditPlan, catalog: PlanningCatalog) -> dict[str, Mapping[str, Any]]:
    """clip_instance_id -> the audio features (mean_db, active_ratio, silent, wind_likely, ...) of its moment, when measured."""
    out: dict[str, Mapping[str, Any]] = {}
    for s in plan.scenes:
        for c in s.clips:
            m = catalog.moments.get(c.moment_id)
            a = (m.features or {}).get("audio") if m is not None else None
            if isinstance(a, Mapping) and a.get("has_audio", True):
                out[c.clip_instance_id] = a
    return out


def _audible(a: Mapping[str, Any] | None, active_min: float) -> bool:
    return bool(a) and not a.get("silent") and a.get("mean_db") is not None and (a.get("active_ratio") or 0.0) >= active_min


def check_audio_discontinuity(plan: EditPlan, clip_audio: Mapping[str, Mapping[str, Any]], jump_db: float = 12.0,
                              active_ratio_min: float = 0.5, ambient_gain_db: float = -12.0) -> CheckResult:
    """Warn at a cut between two video clips that both keep their source audio, when both are audibly active and their mean
    levels (per-moment `mean_db`, plus the constant gain of an `ambient_low` clip) differ by at least `jump_db`. The levels are
    whole-moment averages, so this finds abrupt level changes between takes, not clicks at the cut."""
    name = "audio_discontinuity"
    if not clip_audio:
        return _skipped(name, "no audio measurements (the audio stage did not run, or the clips have no audio)")
    fps = Fraction(plan.sequence.fps)
    ordered = _primary_clips(plan)
    found: list[dict[str, Any]] = []
    compared = 0

    def level(c: Any) -> float | None:
        a = clip_audio.get(c.clip_instance_id)
        if not _audio_kept(c) or not _audible(a, active_ratio_min):
            return None
        return float(a["mean_db"]) + (ambient_gain_db if c.audio_policy == "ambient_low" else 0.0)  # type: ignore[index]

    for (sa, a), (sb, b) in zip(ordered, ordered[1:]):
        la, lb = level(a), level(b)
        if la is None or lb is None or _continues(a, b, fps):
            continue
        compared += 1
        if abs(la - lb) >= jump_db:
            found.append({"between": [a.clip_instance_id, b.clip_instance_id], "scenes": [sa, sb],
                          "at_frame": b.timeline_start_frame, "level_db": [round(la, 1), round(lb, 1)], "jump_db": round(abs(la - lb), 1)})
    if not found:
        return CheckResult(name, PASS, f"{compared} cut(s) between audible clips; none with a level jump of {jump_db:g} dB or more",
                           {"cuts_compared": compared})
    f = found[0]
    return CheckResult(name, WARN, f"{len(found)} cut(s) with a source-audio level jump of {jump_db:g} dB or more; "
                       f"e.g. {f['between'][0]} -> {f['between'][1]} ({f['jump_db']:g} dB)",
                       {"findings": found[:30], "total": len(found), "cuts_compared": compared})


def _audio_kept(c: Any) -> bool:
    return c.media_type == "video" and c.audio_policy != "muted"


def _gain_db(c: Any, ambient_gain_db: float) -> float:
    return ambient_gain_db if c.audio_policy == "ambient_low" else 0.0


def check_music_vs_speech(plan: EditPlan, clip_audio: Mapping[str, Mapping[str, Any]],
                          transcripts: Mapping[str, Sequence[TranscriptSpan]] | None = None, *, ambient_gain_db: float = -12.0,
                          active_ratio_min: float = 0.5, max_gain_db: float = -6.0,
                          duck_windows: Sequence[tuple[int, int]] = (), duck_db: float = 0.0) -> CheckResult:
    """Warn when a music clip plays under a video clip with speech (transcript) or active natural sound (audio features) and the
    plan's own levels do not put the music at least `-max_gain_db` dB below that clip: music level minus natural level, where an
    `ambient_low` clip has the constant gain `ambient_gain_db` and anything else unity. Nothing is invented: the plan generates no
    ducking, so `audio.duck_music_under_speech` does not count as lowering the music. When ducking IS generated (`filmcraft.duck_music`),
    the caller passes the duck windows (sequence frames) and depth: an overlap that lies inside one window counts as lowered by `duck_db`."""
    name = "music_vs_speech"
    music = [(s.scene_id, c) for s in plan.scenes for c in s.clips if c.track_role == "music" and c.media_type == "audio"]
    if not music:
        return _skipped(name, "the plan has no music track")
    fps = Fraction(plan.sequence.fps)
    found: list[dict[str, Any]] = []
    checked = 0
    audible_assets = {c.asset_id for _, c in _primary_clips(plan) if _audible_video(c)}
    coarse_only = any((transcripts or {}).get(a) for a in audible_assets) and not any(
        s.text.strip() and not s.coarse for a in audible_assets for s in (transcripts or {}).get(a, ()))
    coarse_assets = sorted(a for a in audible_assets if (transcripts or {}).get(a) and not any(
        s.text.strip() and not s.coarse for s in (transcripts or {})[a]))
    note = (f"; not checked for speech (transcript without segment timing): {', '.join(coarse_assets)}" if coarse_assets else "")
    if coarse_only and not clip_audio:
        return _skipped(name, "the transcripts have no segment timing (coarse timestamps) and there are no audio measurements, so music "
                        "cannot be compared with speech")
    for ms, m in music:
        if m.audio_policy == "muted":
            continue
        m_start, m_end = m.timeline_start_frame, m.timeline_start_frame + m.duration_frames
        for vs, v in _primary_clips(plan):
            if not _audible_video(v):
                continue
            lo, hi = max(m_start, v.timeline_start_frame), min(m_end, v.timeline_start_frame + v.duration_frames)
            if hi <= lo:
                continue
            checked += 1
            s_lo = v.source_in_seconds + float((lo - v.timeline_start_frame) / fps)
            s_hi = v.source_in_seconds + float((hi - v.timeline_start_frame) / fps)
            speech = [s for s in (transcripts or {}).get(v.asset_id, ()) if s.text.strip() and not s.coarse and s.start < s_hi - 0.1 and s.end > s_lo + 0.1]
            feat = clip_audio.get(v.clip_instance_id)
            reason = "speech" if speech else "active_sound" if _audible(feat, active_ratio_min) else None
            if reason is None:
                continue
            rel = _gain_db(m, ambient_gain_db) - _gain_db(v, ambient_gain_db)
            if duck_db and any(a <= lo and hi <= b for a, b in duck_windows):
                rel += duck_db
            if rel > max_gain_db:
                found.append({"music_clip_instance_id": m.clip_instance_id, "music_scene_id": ms, **_clip_ref(vs, v), "reason": reason,
                              "overlap_seconds": round(float((hi - lo) / fps), 2), "music_gain_db": _gain_db(m, ambient_gain_db),
                              "natural_gain_db": _gain_db(v, ambient_gain_db), "relative_db": rel,
                              "speech": " ".join(s.text.strip() for s in speech)[:120] if speech else None})
    if not found:
        return CheckResult(name, PASS, f"music is not over speech or active sound, or sits {-max_gain_db:g} dB or more below it "
                           f"({checked} overlap(s) checked)" + note,
                           {"overlaps_checked": checked, **({"coarse_transcript_assets": coarse_assets} if coarse_assets else {})})
    f = found[0]
    return CheckResult(name, WARN, f"music {f['music_clip_instance_id']} plays over {f['reason'].replace('_', ' ')} in {f['clip_instance_id']} "
                       f"without being lowered ({len(found)} overlap(s)); lower the music or the clip gain in the edit" + ("" if duck_db else ", no ducking is generated") + note,
                       {"findings": found[:30], "total": len(found), "max_relative_db": max_gain_db,
                        **({"coarse_transcript_assets": coarse_assets} if coarse_assets else {})})


def check_scene_order(plan: EditPlan, outline_scene_ids: Sequence[str] | None) -> CheckResult:
    """The deterministic part of 'scene order is understandable': the plan's scenes play in the approved outline's order (when an
    outline exists), each scene once, and in plan order on the timeline. Whether the story reads well stays a reviewer's call."""
    name = "scene_order"
    if outline_scene_ids is None:
        return _skipped(name, "no approved story outline for this plan (baseline planner, or the outline changed since the plan)")
    plan_ids = [s.scene_id for s in plan.scenes]
    problems: list[str] = []
    data: dict[str, Any] = {"plan_scenes": plan_ids, "outline_scenes": list(outline_scene_ids)}
    dup = sorted({i for i in plan_ids if plan_ids.count(i) > 1})
    unknown = [i for i in plan_ids if i not in outline_scene_ids]
    if dup:
        problems.append(f"scene(s) used more than once: {', '.join(dup)}")
    if unknown:
        problems.append(f"scene(s) not in the outline: {', '.join(unknown)}")
    rank = {sid: n for n, sid in reversed(list(enumerate(outline_scene_ids)))}
    known = [i for i in plan_ids if i in rank]
    inversions = [(a, b) for a, b in zip(known, known[1:]) if rank[a] > rank[b]]
    if inversions:
        problems.append("out of outline order: " + ", ".join(f"{a} before {b}" for a, b in inversions[:5]))
        data["inversions"] = [list(x) for x in inversions[:20]]
    starts = [s.start_frame for s in plan.scenes]
    if starts != sorted(starts):
        problems.append("scene start frames do not increase in plan order")
    omitted = [i for i in outline_scene_ids if i not in plan_ids]
    if omitted:
        data["outline_scenes_not_in_plan"] = omitted   # information only: the planner may legitimately drop a scene
    if problems:
        return CheckResult(name, WARN, "; ".join(problems), data)
    return CheckResult(name, PASS, f"{len(plan_ids)} scene(s) follow the approved outline order"
                       + (f" ({len(omitted)} outline scene(s) not used)" if omitted else ""), data)
