from __future__ import annotations

from fractions import Fraction

import pytest

from conftest import ffprobe_json
from trip_vlog.media.probe import ProbeResult, parse_capture_time, parse_ffprobe_json, parse_iso6709, parse_rational


@pytest.mark.parametrize("raw,expected", [
    ("30/1", Fraction(30)), ("30000/1001", Fraction(30000, 1001)), ("25", Fraction(25)), ("60000/1001", Fraction(60000, 1001)),
    ("0/0", None), ("0/1", None), ("", None), (None, None), ("abc", None), ("-5/1", None), ("1/0", None),
])
def test_parse_rational(raw, expected):
    assert parse_rational(raw) == expected


def test_ntsc_rate_is_exact_not_float():
    fr = parse_rational("30000/1001")
    assert (fr.numerator, fr.denominator) == (30000, 1001) and fr != Fraction(29.97)


def test_landscape():
    r = parse_ffprobe_json(ffprobe_json("landscape.json"), "video")
    assert r.status == "ok" and r.media_type == "video"
    assert (r.width, r.height, r.display_width, r.display_height) == (1920, 1080, 1920, 1080)
    assert r.frame_rate == Fraction(30) and not r.is_vfr and r.rotation_degrees == 0
    assert r.duration_seconds == 5.0 and r.audio_streams == 1 and r.codec == "h264" and r.pix_fmt == "yuv420p"
    assert r.hdr is None


def test_capture_time_prefers_quicktime_local_offset_and_gps_is_parsed():
    r = parse_ffprobe_json(ffprobe_json("landscape.json"), "video")
    assert r.capture_time == "2026-10-31T06:22:10-10:00"
    assert r.capture_time_source == "com.apple.quicktime.creationdate"
    assert r.gps == {"latitude": 21.3069, "longitude": -157.8583, "altitude": 12.3, "source": "com.apple.quicktime.location.iso6709"}


def test_rotated_phone_displaymatrix():
    r = parse_ffprobe_json(ffprobe_json("rotated_phone.json"), "video")
    assert (r.width, r.height) == (1920, 1080)
    assert r.rotation_degrees == 90  # ffprobe says -90 (counter-clockwise needed) -> 90 clockwise
    assert (r.display_width, r.display_height) == (1080, 1920)
    assert r.frame_rate == Fraction(30000, 1001) and not r.is_vfr
    assert r.capture_time == "2026-10-31T16:22:10+00:00" and r.capture_time_source == "creation_time"
    assert any("rotated 90" in n for n in r.notes)


def test_rotate_tag_legacy():
    r = parse_ffprobe_json(ffprobe_json("rotated_legacy_tag.json"), "video")
    assert r.rotation_degrees == 270 and (r.display_width, r.display_height) == (720, 1280)
    assert r.audio_streams == 0 and "no audio stream" in r.notes


def test_displaymatrix_180_and_positive():
    d = ffprobe_json("rotated_phone.json")
    d["streams"][0]["side_data_list"][0]["rotation"] = 180
    r = parse_ffprobe_json(d, "video")
    assert r.rotation_degrees == 180 and (r.display_width, r.display_height) == (1920, 1080)
    d["streams"][0]["side_data_list"][0]["rotation"] = 90
    assert parse_ffprobe_json(d, "video").rotation_degrees == 270


def test_vfr_flagged_and_rational_preserved():
    r = parse_ffprobe_json(ffprobe_json("vfr.json"), "video")
    assert r.is_vfr
    assert r.frame_rate == Fraction(2400000, 101333)
    assert any("variable frame rate" in n for n in r.notes)


def test_ntsc_cfr_not_flagged_vfr():
    d = ffprobe_json("landscape.json")
    d["streams"][0].update(r_frame_rate="30000/1001", avg_frame_rate="2997/100")
    assert not parse_ffprobe_json(d, "video").is_vfr


def test_hdr_hlg_and_pq():
    hlg = parse_ffprobe_json(ffprobe_json("hdr.json"), "video")
    assert hlg.hdr == "hlg" and hlg.pix_fmt == "yuv420p10le" and hlg.frame_rate == Fraction(60000, 1001)
    assert any("HDR" in n for n in hlg.notes)
    pq = parse_ffprobe_json(ffprobe_json("hdr_pq.json"), "video")
    assert pq.hdr == "pq"


def test_high_frame_rate_note():
    d = ffprobe_json("landscape.json")
    d["streams"][0].update(r_frame_rate="240/1", avg_frame_rate="240/1")
    assert any("high frame rate" in n for n in parse_ffprobe_json(d, "video").notes)


def test_image_json():
    r = parse_ffprobe_json(ffprobe_json("image.json"), "image")
    assert r.media_type == "image" and r.duration_seconds is None and r.frame_rate is None
    assert (r.width, r.height) == (4032, 3024) and r.audio_streams == 0
    # also recognised without a hint
    assert parse_ffprobe_json(ffprobe_json("image.json")).media_type == "image"


def test_audio_with_cover_art_is_audio():
    r = parse_ffprobe_json(ffprobe_json("audio.json"), "audio")
    assert r.media_type == "audio" and r.duration_seconds == 61.2 and r.audio_streams == 1 and r.width is None


def test_corrupt_and_empty_fail_with_message():
    r = parse_ffprobe_json(ffprobe_json("corrupt.json"), "video")
    assert r.status == "failed" and "Invalid data" in r.error
    r = parse_ffprobe_json(ffprobe_json("no_streams.json"), "video")
    assert r.status == "failed" and "no media streams" in r.error
    assert parse_ffprobe_json({}, "video").status == "failed"
    assert parse_ffprobe_json([], "video").status == "failed"  # type: ignore[arg-type]


def test_bad_dimensions_fail_gracefully():
    d = ffprobe_json("landscape.json")
    d["streams"][0]["width"] = "wide"
    assert parse_ffprobe_json(d, "video").status == "failed"


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-31T16:22:10-1000", ("2026-10-31T16:22:10-10:00", True)),
    ("2026-10-31T16:22:10+05:30", ("2026-10-31T16:22:10+05:30", True)),
    ("2026-10-31T16:22:10.000000Z", ("2026-10-31T16:22:10+00:00", True)),
    ("2026-10-31 16:22:10", ("2026-10-31T16:22:10", False)),  # local time stays naive, never converted to UTC
    ("2026:10:31 16:22:10", ("2026-10-31T16:22:10", False)),
    ("1970-01-01T00:00:00.000000Z", None), ("0000-00-00 00:00:00", None), ("2026-13-45T00:00:00", None), ("garbage", None), (None, None),
])
def test_parse_capture_time(raw, expected):
    assert parse_capture_time(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("+37.7749-122.4194+012.300/", {"latitude": 37.7749, "longitude": -122.4194, "altitude": 12.3}),
    ("-33.8688+151.2093/", {"latitude": -33.8688, "longitude": 151.2093}),
    ("+91.0+000.0/", None), ("nonsense", None), (None, None),
])
def test_parse_iso6709(raw, expected):
    assert parse_iso6709(raw) == expected


def test_probe_result_roundtrip_through_json():
    import json

    r = parse_ffprobe_json(ffprobe_json("vfr.json"), "video")
    back = ProbeResult.from_dict(json.loads(json.dumps(r.to_dict())))
    assert back == r and isinstance(back.frame_rate, Fraction)
