"""Rational frame math (DESIGN §13.2): no float accumulation, rounding only here, never by an LLM.

Seconds are floats at the edges (JSON, catalog); everything in between is `Fraction` or integer frames.
Timeline positions are always computed from integer frame counts.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Iterable, Literal

RoundMode = Literal["round", "floor", "ceil"]

# Broadcast rates that are routinely written as rounded decimals; snapped to the exact rational.
_NTSC = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001), Fraction(120000, 1001))
_SNAP_TOLERANCE = Fraction(1, 500)  # 0.002 fps
_EPS = Fraction(1, 1000)  # snap values within 1/1000 frame of an integer (float noise, 6-decimal JSON seconds)


def parse_fps(value: Any) -> Fraction:
    """'30', 30, '30000/1001', '29.97' -> Fraction. Decimals near an NTSC rate snap to the exact rational."""
    if isinstance(value, bool):
        raise ValueError("frame rate must be a number or rational")
    try:
        fr = value if isinstance(value, Fraction) else Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"invalid frame rate: {value!r}") from None
    if not (0 < fr <= 1000):
        raise ValueError(f"frame rate out of range: {value!r}")
    for ntsc in _NTSC:
        if fr != ntsc and abs(fr - ntsc) <= _SNAP_TOLERANCE:
            return ntsc
    return fr


def fps_string(fps: Fraction) -> str:
    """'30' for integral rates, else 'num/den' (e.g. '30000/1001')."""
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def _to_fraction(seconds: float | int | Fraction) -> Fraction:
    if isinstance(seconds, Fraction):
        return seconds
    if isinstance(seconds, float) and not math.isfinite(seconds):
        raise ValueError(f"non-finite time: {seconds!r}")
    return Fraction(seconds)


def _round_fraction(x: Fraction, mode: RoundMode) -> int:
    nearest = round(x)  # banker's rounding on exact ties is avoided below
    if abs(x - nearest) <= _EPS:
        return int(nearest)  # float noise: 3.0000000000000001 -> 3
    if mode == "floor":
        return math.floor(x)
    if mode == "ceil":
        return math.ceil(x)
    return math.floor(x + Fraction(1, 2))  # half up


def seconds_to_frames(seconds: float | int | Fraction, fps: Fraction, mode: RoundMode = "round") -> int:
    """Convert seconds to an integer frame count at `fps` (rational arithmetic)."""
    return _round_fraction(_to_fraction(seconds) * fps, mode)


def frames_to_seconds(frames: int, fps: Fraction) -> Fraction:
    return Fraction(frames) / fps


def frames_to_float(frames: int, fps: Fraction) -> float:
    """Seconds as a float for JSON output (rounded to 6 decimals to keep files stable and readable)."""
    return round(float(Fraction(frames) / fps), 6)


def cumulative_starts(durations_frames: Iterable[int]) -> list[int]:
    """Start frame of each item when laid end to end. Pure integer sums: no drift."""
    out: list[int] = []
    pos = 0
    for d in durations_frames:
        out.append(pos)
        pos += int(d)
    return out


def total_frames(durations_frames: Iterable[int]) -> int:
    return sum(int(d) for d in durations_frames)


def frame_distance(seconds: float, fps: Fraction) -> Fraction:
    """Distance (in frames) from `seconds` to the nearest frame boundary; 0 means frame-aligned."""
    x = _to_fraction(seconds) * fps
    return abs(x - round(x))


def sequence_dimensions(aspect_ratio: str, short_side: int = 1080) -> tuple[int, int]:
    """Width/height for an 'A:B' aspect ratio. The short side is `short_side`; both are even.

    16:9 -> 1920x1080, 9:16 -> 1080x1920, 4:3 -> 1440x1080, 1:1 -> 1080x1080.
    """
    a_s, _, b_s = aspect_ratio.partition(":")
    a, b = int(a_s), int(b_s)
    if a <= 0 or b <= 0:
        raise ValueError(f"invalid aspect ratio: {aspect_ratio!r}")

    def even(x: Fraction) -> int:
        n = int(math.floor(x + Fraction(1, 2)))
        return n + (n % 2)

    if a >= b:
        return even(Fraction(short_side * a, b)), short_side
    return short_side, even(Fraction(short_side * b, a))
