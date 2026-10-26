"""Mixed-aspect framing check (DESIGN §20 Phase 7): clips and stills whose size differs from the sequence were scaled to fit the frame.

Two independent looks, both warn-only like the other heuristics:

  * read-back: the Motion scale FilmCraft reports for each scaled clip after save and reopen must be the one that fits the source in the
    frame (``fit_scale_percent``);
  * preview: one frame from the middle of each scaled clip is taken from the rendered preview and the bars the fit leaves (left/right for a
    narrower picture, top/bottom for a wider one) must be black. A frame is skipped where something else is on screen over the clip (an
    overlay on V2, a title, a dissolve) because that would not be the clip alone. A source that fills the frame is not measured (no bars).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from trip_vlog.filmcraft.inspect import FIT_SCALE_TOLERANCE, SequenceSnapshot
from trip_vlog.filmcraft.timeline_spec import Placement, TimelineSpec, fit_scale_percent
from trip_vlog.media.tools import popen_flags
from trip_vlog.qc.checks import PASS, SKIPPED, WARN, CheckResult

BAR_MAX_LUMA = 40          # 8-bit luma; limited-range black is 16
BAR_MARGIN_PX = 4          # stay clear of the scaled picture's edge (resampling blur)
MIN_BAR_PX = 8
MAX_SAMPLES = 8

_YMAX = re.compile(r"lavfi\.signalstats\.YMAX=([0-9.]+)")


def bar_regions(seq_w: int, seq_h: int, src_w: int, src_h: int, scale: float) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) of the bars a picture of ``src_w`` x ``src_h`` scaled by ``scale`` percent leaves in the frame (empty: it fills it)."""
    cw, ch = src_w * scale / 100.0, src_h * scale / 100.0
    out: list[tuple[int, int, int, int]] = []
    side = int((seq_w - cw) / 2) - BAR_MARGIN_PX
    if side >= MIN_BAR_PX:
        out += [(0, 0, side, seq_h), (seq_w - side, 0, side, seq_h)]
    band = int((seq_h - ch) / 2) - BAR_MARGIN_PX
    if band >= MIN_BAR_PX:
        out += [(0, 0, seq_w, band), (0, seq_h - band, seq_w, band)]
    return out


def region_max_luma(ffmpeg: Path | str, preview: Path | str, at_seconds: float, region: tuple[int, int, int, int]) -> float | None:
    x, y, w, h = region
    try:
        cp = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-ss", f"{at_seconds:.3f}", "-i", str(preview), "-frames:v", "1",
                             "-vf", f"crop={w}:{h}:{x}:{y},signalstats,metadata=print:key=lavfi.signalstats.YMAX:file=-", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, **popen_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    m = _YMAX.search(cp.stdout or "")
    return float(m[1]) if m else None


def _alone(spec: TimelineSpec, p: Placement, frame: int) -> bool:
    """Nothing but this clip is visible at ``frame``: no other picture, title or dissolve is on screen."""
    for o in spec.placements:
        if o is p or o.media_kind == "audio":
            continue
        if o.timeline_start_frame <= frame < o.end_frame:
            return False
        ti = o.transition_in
        if ti.type == "dissolve" and abs(frame - o.timeline_start_frame) <= ti.frames:
            return False
    return not any(t.start_frame <= frame < t.end_frame for t in spec.titles)


def check_framing(spec: TimelineSpec, seq: SequenceSnapshot | None, preview: Path | None, ffmpeg: Path | str | None,
                  *, preview_scale: float = 1.0) -> CheckResult:
    name = "framing"
    fit = [p for p in spec.placements if p.fit_source_size is not None]
    if not fit:
        return CheckResult(name, SKIPPED, "skipped: no clip was scaled to the frame (filmcraft.fit_mixed_aspect is off, or every source has the sequence size)",
                           {"skipped": True, "reason": "none"})
    findings: list[dict[str, Any]] = []
    read_back = 0
    if seq is not None:
        for p in fit:
            c = next((c for c in (seq.track(p.track).clips if seq.track(p.track) else ())
                      if c.start_frame == p.timeline_start_frame and c.kind == "video"), None)
            want = round(fit_scale_percent(spec.width, spec.height, *p.fit_source_size), 2)
            if c is None:
                continue
            read_back += 1
            if c.scale is None or abs(c.scale - want) > FIT_SCALE_TOLERANCE:
                findings.append({"clip_instance_id": p.clip_instance_id, "where": "read_back", "scale": c.scale, "expected_scale": want})
    frames_checked = 0
    if preview is not None and Path(preview).is_file() and ffmpeg is not None:
        sw, sh = max(1, round(spec.width * preview_scale)), max(1, round(spec.height * preview_scale))
        sampled = 0
        for p in fit:
            if sampled >= MAX_SAMPLES:
                break
            scale = fit_scale_percent(sw, sh, *p.fit_source_size)
            regions = bar_regions(sw, sh, p.fit_source_size[0], p.fit_source_size[1], scale)
            mid = p.timeline_start_frame + p.duration_frames // 2
            if not regions or not _alone(spec, p, mid):
                continue
            sampled += 1
            worst = None
            for r in regions:
                y = region_max_luma(ffmpeg, preview, float(mid / spec.fps), r)
                if y is not None:
                    worst = y if worst is None else max(worst, y)
            if worst is None:
                continue
            frames_checked += 1
            if worst > BAR_MAX_LUMA:
                findings.append({"clip_instance_id": p.clip_instance_id, "where": "preview", "frame": mid, "bar_max_luma": worst})
    if findings:
        f = findings[0]
        return CheckResult(name, WARN, f"{len(findings)} scaled clip(s) do not look framed as planned; e.g. {f['clip_instance_id']} ({f['where']})",
                           {"findings": findings[:30], "total": len(findings), "scaled": len(fit), "read_back": read_back, "preview_frames": frames_checked})
    parts = [f"{read_back} of {len(fit)} scaled clip(s) read back with the fitting scale"]
    parts.append(f"{frames_checked} preview frame(s) have black bars" if frames_checked else "no preview frame could be checked for bars (all fill the frame, "
                 "have something over them, or ffmpeg/preview is missing)")
    return CheckResult(name, PASS, "; ".join(parts), {"scaled": len(fit), "read_back": read_back, "preview_frames": frames_checked})
