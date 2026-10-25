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

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # pass | warn | fail
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
    return WARN if any(r.status == WARN for r in results) else PASS


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
