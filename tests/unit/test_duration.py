from fractions import Fraction

import pytest

from trip_vlog.planning.duration import (cumulative_starts, fps_string, frame_distance, frames_to_float, frames_to_seconds, parse_fps,
                                         seconds_to_frames, sequence_dimensions, total_frames)

NTSC = Fraction(30000, 1001)


def test_parse_fps_forms_and_ntsc_snapping():
    assert parse_fps("30") == 30 and parse_fps(25) == 25 and parse_fps("30000/1001") == NTSC
    assert parse_fps("29.97") == NTSC and parse_fps(23.976) == Fraction(24000, 1001) and parse_fps("59.94") == Fraction(60000, 1001)
    assert parse_fps("29.5") == Fraction(59, 2)  # not near an NTSC rate: kept exact
    for bad in ("0", "-1", "abc", "30/0", True, "5000"):
        with pytest.raises(ValueError):
            parse_fps(bad)


def test_fps_string():
    assert fps_string(Fraction(30)) == "30" and fps_string(NTSC) == "30000/1001" and fps_string(Fraction(25)) == "25"


@pytest.mark.parametrize("fps,secs,mode,frames", [
    (Fraction(25), 1.0, "round", 25), (Fraction(25), 0.019, "round", 0), (Fraction(25), 0.02, "round", 1), (Fraction(25), 0.03, "floor", 0),
    (Fraction(25), 0.03, "ceil", 1), (NTSC, 1.001, "round", 30), (NTSC, 10.0, "floor", 299), (NTSC, 10.0, "ceil", 300),
    (NTSC, 0.5, "round", 15), (Fraction(30), 0.1, "round", 3), (Fraction(30), 0.1 * 3, "floor", 9),  # 0.30000000000000004
])
def test_seconds_to_frames(fps, secs, mode, frames):
    assert seconds_to_frames(secs, fps, mode) == frames


def test_float_noise_and_six_decimal_json_seconds_snap_to_frames():
    # 1/NTSC = 0.0333666..s is stored as 0.033367 in JSON; ceil must still give 1 frame, not 2
    assert seconds_to_frames(0.033367, NTSC, "ceil") == 1 and seconds_to_frames(0.033367, NTSC, "floor") == 1
    assert seconds_to_frames(frames_to_float(2997, NTSC), NTSC, "ceil") == 2997


def test_round_half_up_not_bankers():
    assert seconds_to_frames(0.5, Fraction(5), "round") == 3  # 2.5 -> 3
    assert seconds_to_frames(1.5, Fraction(1), "round") == 2 and seconds_to_frames(2.5, Fraction(1), "round") == 3


def test_non_finite_rejected():
    with pytest.raises(ValueError):
        seconds_to_frames(float("nan"), Fraction(30))
    with pytest.raises(ValueError):
        seconds_to_frames(float("inf"), Fraction(30))


def test_frames_to_seconds_exact_and_float():
    assert frames_to_seconds(30000, NTSC) == Fraction(1001)
    assert frames_to_float(1, NTSC) == 0.033367 and frames_to_float(90, Fraction(30)) == 3.0


def test_positions_from_frame_counts_do_not_drift():
    durations = [seconds_to_frames(0.1, NTSC) or 1] * 100_000  # 3 frames each, ~100k additions
    starts = cumulative_starts(durations)
    assert starts[-1] == 3 * 99_999 and total_frames(durations) == 300_000
    # repeated float addition of 1/NTSC drifts away from the exact rational; integer frame math does not
    acc = 0.0
    for _ in range(300_000):
        acc += 1 / float(NTSC)
    exact = frames_to_seconds(300_000, NTSC)
    assert abs(Fraction(acc) - exact) > 0 and frames_to_float(300_000, NTSC) == float(exact)


def test_frame_distance():
    assert frame_distance(1.0, Fraction(25)) == 0
    assert float(frame_distance(0.03, Fraction(25))) == pytest.approx(0.25)


@pytest.mark.parametrize("aspect,dims", [("16:9", (1920, 1080)), ("9:16", (1080, 1920)), ("4:3", (1440, 1080)), ("1:1", (1080, 1080)),
                                          ("21:9", (2520, 1080)), ("3:4", (1080, 1440))])
def test_sequence_dimensions(aspect, dims):
    assert sequence_dimensions(aspect) == dims
    assert all(d % 2 == 0 for d in dims)
