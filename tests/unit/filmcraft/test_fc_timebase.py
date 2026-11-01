from __future__ import annotations

from fractions import Fraction

import pytest

from trip_vlog.filmcraft.timebase import TICKS_PER_SECOND, Timebase, fps_command_value, normalize_fps, to_fraction


@pytest.mark.parametrize("fps,ticks", [
    (Fraction(30), 8_467_200_000),
    (Fraction(30000, 1001), 8_475_667_200),
    (Fraction(25), 10_160_640_000),
    (Fraction(24000, 1001), 10_594_584_000),
    (Fraction(60), 4_233_600_000),
    (Fraction(60000, 1001), 4_237_833_600),
])
def test_ticks_per_frame_is_exact_integer(fps, ticks):
    tb = Timebase(fps)
    assert tb.ticks_per_frame == ticks and tb.ticks_per_frame_is_integral
    assert tb.frames_to_ticks(1) == ticks
    assert tb.frames_to_ticks(105) == 105 * ticks


@pytest.mark.parametrize("fps", [Fraction(30), Fraction(30000, 1001), Fraction(25), Fraction(24000, 1001)])
def test_frames_ticks_roundtrip_is_exact_over_a_long_timeline(fps):
    tb = Timebase(fps)
    for frames in (0, 1, 7, 105, 5399, 107_999, 1_000_003):
        assert tb.ticks_to_frames(tb.frames_to_ticks(frames)) == frames
        assert tb.ticks_to_frames_exact(tb.frames_to_ticks(frames)) == frames


def test_no_accumulated_drift_for_ntsc():
    tb = Timebase(Fraction(30000, 1001))
    assert sum(tb.frames_to_ticks(1) for _ in range(10_000)) == tb.frames_to_ticks(10_000)
    # one hour of 29.97 is NOT 108000 frames of 30 fps: exact rational math keeps them apart
    assert tb.frames_to_ticks(107_892) == 107_892 * 254_016_000_000 * 1001 // 30000


def test_seconds_use_shortest_decimal_not_binary_float():
    tb = Timebase(30)
    assert tb.seconds_to_ticks(0.1) == TICKS_PER_SECOND // 10
    assert tb.seconds_to_ticks(2.2) == 2 * TICKS_PER_SECOND + TICKS_PER_SECOND // 5
    assert tb.seconds_to_frames(0.5) == 15
    assert tb.seconds_to_frames("0.0166667") == 1
    assert tb.ticks_to_seconds(TICKS_PER_SECOND * 3) == 3


def test_non_integral_tick_rate_rounds_to_nearest_tick():
    tb = Timebase(Fraction(11))  # 254016000000 / 11 is not an integer
    assert not tb.ticks_per_frame_is_integral
    assert tb.frames_to_ticks(11) == TICKS_PER_SECOND
    assert abs(tb.frames_to_ticks(1) - TICKS_PER_SECOND / 11) <= 0.5


def test_normalize_fps_snaps_ntsc_decimals_only():
    assert normalize_fps("29.97") == Fraction(30000, 1001)
    assert normalize_fps(23.976) == Fraction(24000, 1001)
    assert normalize_fps(59.94) == Fraction(60000, 1001)
    assert normalize_fps("30000/1001") == Fraction(30000, 1001)
    assert normalize_fps(25) == 25 and normalize_fps("12.5") == Fraction(25, 2)
    with pytest.raises(ValueError):
        normalize_fps(0)


def test_fps_command_value():
    assert fps_command_value(Fraction(30)) == 30 and isinstance(fps_command_value(Fraction(30)), int)
    assert fps_command_value(Fraction(30000, 1001)) == pytest.approx(29.97002997)


def test_type_errors():
    with pytest.raises(TypeError):
        Timebase(30).frames_to_ticks(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_fraction(True)
    with pytest.raises(ValueError):
        to_fraction(float("nan"))
