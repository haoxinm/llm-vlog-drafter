"""Exact frame / tick / second conversions. This is the ONLY place frames become ticks (DESIGN §13.2).

FilmCraft time is an integer tick count, 254 016 000 000 ticks per second (documented, re-verified in
Phase 0). A frame of an ``n/d`` fps sequence is ``254016000000 * d / n`` ticks. For every common rate
(24, 25, 30, 50, 60 and the NTSC family 24000/1001 ... 60000/1001) that is an exact integer; for any
other rate the value is rounded to the nearest tick (FilmCraft snaps clips to its frame grid anyway).
All arithmetic uses ``fractions.Fraction`` - no float accumulation.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Union

TICKS_PER_SECOND = 254_016_000_000

Number = Union[int, float, str, Fraction]

_NTSC = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(48000, 1001), Fraction(60000, 1001),
         Fraction(120000, 1001))


def to_fraction(value: Number) -> Fraction:
    """Exact Fraction from an int/str/Fraction, or from a float via its shortest decimal repr."""
    if isinstance(value, bool):
        raise TypeError("bool is not a number")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number: {value!r}")
        return Fraction(repr(value))
    return Fraction(str(value).strip())


def normalize_fps(value: Number) -> Fraction:
    """Frame rate as an exact Fraction. Decimal NTSC approximations (29.97, 23.976 ...) snap to n/1001."""
    fps = to_fraction(value)
    if fps <= 0:
        raise ValueError(f"frame rate must be positive: {value!r}")
    for ntsc in _NTSC:
        if fps != ntsc and abs(float(fps) - float(ntsc)) < 0.0015:
            return ntsc
    return fps


def round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


class Timebase:
    """Conversions for one sequence frame rate."""

    def __init__(self, fps: Number):
        self.fps = normalize_fps(fps)

    @property
    def ticks_per_frame(self) -> Fraction:
        return Fraction(TICKS_PER_SECOND) / self.fps

    @property
    def ticks_per_frame_is_integral(self) -> bool:
        return self.ticks_per_frame.denominator == 1

    def frames_to_ticks(self, frames: int) -> int:
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise TypeError(f"frames must be an int, got {frames!r}")
        return round_half_up(frames * self.ticks_per_frame)

    def frames_to_seconds(self, frames: int) -> Fraction:
        return Fraction(frames) / self.fps

    def seconds_to_ticks(self, seconds: Number) -> int:
        return round_half_up(to_fraction(seconds) * TICKS_PER_SECOND)

    def seconds_to_frames_exact(self, seconds: Number) -> Fraction:
        return to_fraction(seconds) * self.fps

    def seconds_to_frames(self, seconds: Number) -> int:
        """Nearest whole frame (half up)."""
        return round_half_up(self.seconds_to_frames_exact(seconds))

    def ticks_to_seconds(self, ticks: int) -> Fraction:
        return Fraction(ticks, TICKS_PER_SECOND)

    def ticks_to_frames_exact(self, ticks: int) -> Fraction:
        return Fraction(ticks) / self.ticks_per_frame

    def ticks_to_frames(self, ticks: int) -> int:
        """Nearest whole frame (half up)."""
        return round_half_up(self.ticks_to_frames_exact(ticks))


def fps_command_value(fps: Fraction) -> Union[int, float]:
    """Value for ``file.newSequence {fps}``: an int for whole rates, else the float quotient.

    Phase 0 verified that FilmCraft stores 24000/1001, 30000/1001 and 60000/1001 exactly when given the
    float quotient; the adapter re-reads the stored rational and fails if it differs.
    """
    return int(fps) if fps.denominator == 1 else float(fps)
