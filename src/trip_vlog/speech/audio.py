"""Audio extraction (ffmpeg -> 16 kHz mono WAV/FLAC) and cheap deterministic audio features (DESIGN §7.3).

Features come from ONE ffmpeg pass over the audio of an asset: EBU R128 integrated loudness (`ebur128`) and, per 0.25 s window,
the full-band RMS/peak and the RMS of the same signal low-passed at 200 Hz (a second channel built with `asplit`/`lowpass`/
`amerge`). Everything after that is pure arithmetic on those windows (`summarize`), so it is unit-testable without ffmpeg.

Heuristics (all "likely", never certain; thresholds are module constants and part of the cache key):
  silent       power-mean level of the windows below `media.silence_threshold_db` (the same rule as the segmenter).
  clipped      >= 2% of windows peak at >= -0.1 dBFS (same rule as the segmenter's clipped_audio_hint). A single full-scale peak
               in a normalised recording therefore does not count; sustained flat-topped sound does.
  wind_likely  wind and handling rumble put almost all of their energy below ~200 Hz, whereas speech, surf, crowds and music keep a
               large share above it. A window is "low-frequency dominated" when it is not silent and the low-passed level is
               within 1.0 dB of the full-band level (>= ~79% of the power below ~200 Hz; white noise has ~2.5%, speech typically
               well under 50%). A segment/asset is wind_likely when >= 50% of its non-silent windows are low-frequency dominated
               and there are at least 2 of them. A steady engine rumble or deep bass can trigger it too: it is a hint for review.
  active_ratio share of windows above the silence threshold. This is energy, not speech detection.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from trip_vlog.media.segment import CLIP_PEAK_DB, CLIP_WINDOW_FRACTION, FLOOR_DB, audio_features
from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path
from trip_vlog.state import stable_hash

AUDIO_FEATURES_VERSION = 1
WINDOW_SECONDS = 0.25
SAMPLE_RATE = 16000
WIND_CUTOFF_HZ = 200
WIND_LF_MARGIN_DB = 1.0
WIND_WINDOW_FRACTION = 0.5
WIND_MIN_WINDOWS = 2

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")
_KEY_RE = re.compile(r"lavfi\.astats\.(\d)\.(RMS_level|Peak_level)=(\S+)")
_LUFS_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[0-9.]+|-?inf)\s*LUFS", re.I)


def features_key(silence_db: float) -> str:
    return stable_hash({"v": AUDIO_FEATURES_VERSION, "win": WINDOW_SECONDS, "silence_db": silence_db, "cut": WIND_CUTOFF_HZ,
                        "margin": WIND_LF_MARGIN_DB, "frac": WIND_WINDOW_FRACTION, "minw": WIND_MIN_WINDOWS, "clip": [CLIP_PEAK_DB, CLIP_WINDOW_FRACTION]})


def _db(x: float | None) -> float:
    if x is None or not math.isfinite(x):
        return FLOOR_DB
    return max(FLOOR_DB, x)


# ---- pure ----------------------------------------------------------------------------------------

def parse_windows(text: str) -> list[list[float]]:
    """`ametadata=print` output of the two-channel astats graph -> [[t, rms_db, peak_db, lf_rms_db], ...]."""
    out: list[list[float]] = []
    t: float | None = None
    cur: dict[tuple[str, str], float] = {}

    def flush() -> None:
        if t is not None and cur:
            out.append([round(t, 4), round(_db(cur.get(("1", "RMS_level"))), 2), round(_db(cur.get(("1", "Peak_level"))), 2),
                        round(_db(cur.get(("2", "RMS_level"))), 2)])

    for line in text.splitlines():
        m = _PTS_RE.search(line)
        if m:
            flush()
            t, cur = float(m[1]), {}
            continue
        m = _KEY_RE.search(line)
        if m:
            try:
                cur[(m[1], m[2])] = float(m[3])
            except ValueError:
                cur[(m[1], m[2])] = float("-inf")
    flush()
    return out


def parse_lufs(stderr: str) -> float | None:
    m = _LUFS_RE.search(stderr)
    if not m:
        return None
    try:
        v = float(m[1])
    except ValueError:
        return None
    return round(v, 1) if math.isfinite(v) and v > -69.9 else None  # -70 is the gate floor: nothing measurable


def summarize(windows: Sequence[Sequence[float]], start_s: float, end_s: float, silence_db: float) -> dict[str, Any]:
    """Audio flags for [start_s, end_s) from the windows (see the module docstring for the rules)."""
    base = audio_features(windows, start_s, end_s, silence_db)
    inside = [w for w in windows if start_s - 1e-9 <= w[0] < end_s - 1e-9]
    if not inside:
        return {"has_audio": True, "silent": None, "clipped": None, "wind_likely": None, "active_ratio": None,
                "mean_db": None, "peak_db": None, "lf_share_db": None, "audio_tags": []}
    active = [w for w in inside if _db(w[1]) >= silence_db]
    lf = [w for w in active if len(w) > 3 and _db(w[3]) - _db(w[1]) >= -WIND_LF_MARGIN_DB]
    wind = len(lf) >= WIND_MIN_WINDOWS and len(lf) / len(active) >= WIND_WINDOW_FRACTION
    lf_db = None
    if active:  # power-averaged low-band level relative to the full band over the non-silent windows (<= 0 dB)
        p_full = sum(10 ** (_db(w[1]) / 10) for w in active)
        p_lo = sum(10 ** (_db(w[3]) / 10) for w in active if len(w) > 3)
        lf_db = round(10 * math.log10(p_lo / p_full), 2) if p_full > 0 and p_lo > 0 else None
    out: dict[str, Any] = {
        "has_audio": True, "silent": bool(base["silent"]), "clipped": bool(base["clipped_audio_hint"]), "wind_likely": bool(wind),
        "active_ratio": round(len(active) / len(inside), 3), "mean_db": base["audio_mean_db"], "peak_db": base["audio_peak_db"],
        "lf_share_db": lf_db,
    }
    out["audio_tags"] = audio_tags(out)
    return out


def audio_tags(a: dict[str, Any]) -> list[str]:
    """Hints derived only from the measured flags. `clean_audio` = audible, not clipped, not wind-dominated; it says nothing
    about WHAT the sound is (waves, birds, crowd): that stays an editorial judgement."""
    if not a.get("has_audio", True) or a.get("silent"):
        return ["silent"]
    tags = [t for t, on in (("clipped_audio", a.get("clipped")), ("wind_likely", a.get("wind_likely"))) if on]
    return tags or ["clean_audio"]


def no_audio() -> dict[str, Any]:
    return {"has_audio": False, "silent": True, "clipped": False, "wind_likely": False, "active_ratio": 0.0, "mean_db": None,
            "peak_db": None, "lf_share_db": None, "audio_tags": ["silent"]}


# ---- ffmpeg I/O ---------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    cp = subprocess.run(cmd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **popen_flags())
    return cp.returncode, cp.stdout or "", cp.stderr or ""


def measure_audio(ffmpeg: Path, src: Path, duration: float | None) -> dict[str, Any]:
    """One ffmpeg pass -> {windows, lufs}. Raises RuntimeError. Cacheable (JSON-able)."""
    n = round(SAMPLE_RATE * WINDOW_SECONDS)
    head = (f"[0:a:0]ebur128=framelog=quiet,aresample={SAMPLE_RATE},aformat=channel_layouts=mono,asplit=2[f][l];"
            f"[l]lowpass=f={WIND_CUTOFF_HZ}[lo];[f][lo]amerge=inputs=2,asetnsamples=n={n}:p=0,")
    variants = [head + "astats=metadata=1:reset=1:measure_perchannel=RMS_level+Peak_level:measure_overall=none,ametadata=mode=print:file=-",
                head + "astats=metadata=1:reset=1,ametadata=mode=print:file=-"]  # older ffmpeg without measure_* options
    timeout = max(120.0, 3.0 * (duration or 0.0))
    last = "no output"
    for fc in variants:
        cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info", "-i", str(long_path(src.resolve())),
               "-vn", "-sn", "-filter_complex", fc, "-f", "null", "-"]
        try:
            rc, out, err = _run(cmd, timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"audio measurement timed out after {timeout:g}s") from None
        except OSError as e:
            raise RuntimeError(f"cannot run ffmpeg: {e.strerror or e}") from None
        if rc == 0:
            return {"windows": parse_windows(out), "lufs": parse_lufs(err), "window_seconds": WINDOW_SECONDS}
        last = (err.strip().splitlines() or [last])[-1]
    raise RuntimeError(f"audio measurement failed: {last}")


def extract_audio(ffmpeg: Path, src: Path, dest: Path, fmt: str = "wav", *, start: float | None = None,
                  duration: float | None = None, timeout: float = 600.0) -> str | None:
    """Write a 16 kHz mono WAV/FLAC of the first audio stream (optionally a time range) to `dest`. Returns an error or None.
    The file appears atomically (written as .part, then renamed)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    codec = ["-c:a", "pcm_s16le", "-f", "wav"] if fmt == "wav" else ["-c:a", "flac", "-f", "flac"]
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(long_path(src.resolve()))]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-vn", "-sn", "-ac", "1", "-ar", str(SAMPLE_RATE), *codec, str(long_path(part))]
    try:
        rc, _, err = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        part.unlink(missing_ok=True)
        return f"audio extraction timed out after {timeout:g}s"
    except OSError as e:
        return f"cannot run ffmpeg: {e.strerror or e}"
    if rc != 0 or not part.is_file() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        return f"audio extraction failed: {(err.strip().splitlines() or ['no output'])[-1]}"
    os.replace(part, dest)
    return None
