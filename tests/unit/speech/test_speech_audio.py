from __future__ import annotations

import subprocess
import wave

import pytest

from trip_vlog.media.segment import moments_from_segments
from trip_vlog.speech import audio as au

SILENCE_DB = -35.0


def win(n, rms, peak=None, lo=None, t0=0.0):
    """n windows of 0.25 s with the given levels."""
    return [[t0 + 0.25 * i, rms, rms + 6 if peak is None else peak, rms - 20 if lo is None else lo] for i in range(n)]


# ---------------------------------------------------------------- pure heuristics


def test_silent_when_power_mean_below_threshold():
    a = au.summarize(win(8, -60.0), 0, 2, SILENCE_DB)
    assert a["silent"] is True and a["active_ratio"] == 0.0 and a["audio_tags"] == ["silent"] and a["wind_likely"] is False


def test_clean_audio_and_active_ratio():
    w = win(4, -60.0) + win(4, -20.0, t0=1.0)       # half silent, half normal speech-like level
    a = au.summarize(w, 0, 2, SILENCE_DB)
    assert a["silent"] is False and a["clipped"] is False and a["wind_likely"] is False and a["active_ratio"] == 0.5
    assert a["audio_tags"] == ["clean_audio"] and a["peak_db"] == -14.0


def test_clipped_needs_two_percent_of_windows_at_full_scale():
    assert au.summarize(win(100, -20.0, peak=-0.05), 0, 25, SILENCE_DB)["clipped"] is True
    w = win(100, -20.0)
    w[10][2] = 0.0                                   # one full-scale peak in a normalised recording
    assert au.summarize(w, 0, 25, SILENCE_DB)["clipped"] is False
    w[11][2] = w[12][2] = 0.0                        # 3% of windows
    a = au.summarize(w, 0, 25, SILENCE_DB)
    assert a["clipped"] is True and "clipped_audio" in a["audio_tags"] and "clean_audio" not in a["audio_tags"]


def test_wind_needs_low_band_to_dominate_most_active_windows():
    wind = win(8, -22.0, lo=-22.5)                   # 0.5 dB below full band: ~89% of the power under 200 Hz
    assert au.summarize(wind, 0, 2, SILENCE_DB)["wind_likely"] is True
    assert au.summarize(win(8, -22.0, lo=-25.0), 0, 2, SILENCE_DB)["wind_likely"] is False     # 3 dB below: broadband sound
    mixed = win(4, -22.0, lo=-22.2) + win(4, -22.0, lo=-30.0, t0=1.0)                           # exactly half the windows
    assert au.summarize(mixed, 0, 2, SILENCE_DB)["wind_likely"] is True
    assert au.summarize(win(1, -22.0, lo=-22.0), 0, 0.25, SILENCE_DB)["wind_likely"] is False   # a single window is not evidence
    quiet_rumble = win(8, -50.0, lo=-50.0)                                                      # silent rumble is "silent", not wind
    a = au.summarize(quiet_rumble, 0, 2, SILENCE_DB)
    assert a["silent"] is True and a["wind_likely"] is False and a["audio_tags"] == ["silent"]


def test_segment_ranges_use_their_own_windows():
    w = win(8, -22.0, lo=-22.2) + win(8, -22.0, lo=-35.0, t0=2.0)
    assert au.summarize(w, 0, 2, SILENCE_DB)["wind_likely"] is True
    assert au.summarize(w, 2, 4, SILENCE_DB)["wind_likely"] is False
    empty = au.summarize(w, 10, 12, SILENCE_DB)
    assert empty["silent"] is None and empty["wind_likely"] is None and empty["audio_tags"] == []


def test_no_audio_record_and_tags():
    a = au.no_audio()
    assert a["has_audio"] is False and a["silent"] is True and a["audio_tags"] == ["silent"]
    assert au.audio_tags({"has_audio": True, "silent": False, "clipped": True, "wind_likely": True}) == ["clipped_audio", "wind_likely"]


def test_parse_windows_and_lufs():
    text = """frame:0    pts:0       pts_time:0
lavfi.astats.1.Peak_level=-13.168118
lavfi.astats.1.RMS_level=-23.846918
lavfi.astats.2.Peak_level=-15.25
lavfi.astats.2.RMS_level=-inf
frame:1    pts:4000    pts_time:0.25
lavfi.astats.1.Peak_level=-12.0
lavfi.astats.1.RMS_level=-24.0
lavfi.astats.2.Peak_level=-15.0
lavfi.astats.2.RMS_level=-26.0
"""
    assert au.parse_windows(text) == [[0.0, -23.85, -13.17, -120.0], [0.25, -24.0, -12.0, -26.0]]
    log = "x\n  Integrated loudness:\n    I:         -28.4 LUFS\n    Threshold: -38.4 LUFS\n"
    assert au.parse_lufs(log) == -28.4
    assert au.parse_lufs("Integrated loudness:\n    I:         -inf LUFS\n") is None
    assert au.parse_lufs("Integrated loudness:\n    I:         -70.0 LUFS\n") is None
    assert au.parse_lufs("nothing") is None


def test_features_key_depends_on_silence_threshold_only():
    assert au.features_key(-35.0) == au.features_key(-35.0) != au.features_key(-40.0)


def test_baseline_moments_carry_audio_when_present():
    seg = {"segment_id": "seg_0123456789abcdef", "asset_id": "a", "media_type": "video", "start_seconds": 0.0, "end_seconds": 2.0,
           "features": {"silent": False}}
    assert "audio" not in moments_from_segments([seg], "cv")[0]
    seg["features"]["audio"] = {"silent": False, "audio_tags": ["clean_audio"]}
    m = moments_from_segments([seg], "cv")[0]
    assert m["audio"]["silent"] is False and m["audio_tags"] == ["clean_audio"]


# ---------------------------------------------------------------- real ffmpeg on generated fixtures

pytestmark_ffmpeg = pytest.mark.integration


def gen(ffmpeg, path, source, seconds=3, *, filters=None, fmt="pcm_s16le"):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{source}:d={seconds}:r=16000"]
    if filters:
        cmd += ["-af", filters]
    cmd += ["-ac", "1", "-c:a", fmt, str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def measure(ffmpeg, path):
    prof = au.measure_audio(__import__("pathlib").Path(ffmpeg), path, 3.0)
    return au.summarize(prof["windows"], 0, 3.0, SILENCE_DB), prof


@pytest.fixture(scope="module")
def ff(ffmpeg_tools):
    return ffmpeg_tools[0]


def test_real_silent_clip(tmp_path, ff):
    a, prof = measure(ff, gen(ff, tmp_path / "s.wav", "anullsrc=r=16000:cl=mono"))
    assert a["silent"] is True and a["clipped"] is False and a["wind_likely"] is False and a["audio_tags"] == ["silent"]
    assert prof["lufs"] is None and len(prof["windows"]) >= 10


def test_real_clipped_tone(tmp_path, ff):
    # a 440 Hz tone driven far past full scale (+30 dB on the default 1/8 amplitude) and saved as 16-bit PCM: flat-topped, peaks at 0 dBFS in every window
    a, prof = measure(ff, gen(ff, tmp_path / "c.wav", "sine=f=440", filters="volume=30dB"))
    assert a["clipped"] is True and a["silent"] is False and a["wind_likely"] is False and "clipped_audio" in a["audio_tags"]
    assert prof["lufs"] is not None and prof["lufs"] > -15


def test_real_normal_tone_is_clean(tmp_path, ff):
    a, _ = measure(ff, gen(ff, tmp_path / "n.wav", "sine=f=1000", filters="volume=-12dB"))
    assert a == {**a, "silent": False, "clipped": False, "wind_likely": False, "audio_tags": ["clean_audio"], "active_ratio": 1.0}
    assert a["lf_share_db"] < -10


def test_real_low_frequency_noise_is_wind_likely_but_white_noise_is_not(tmp_path, ff):
    wind, _ = measure(ff, gen(ff, tmp_path / "w.wav", "anoisesrc=color=brown:amplitude=0.3", seconds=4))
    assert wind["wind_likely"] is True and wind["silent"] is False and wind["audio_tags"] == ["wind_likely"] and wind["lf_share_db"] > -1.5
    white, _ = measure(ff, gen(ff, tmp_path / "wh.wav", "anoisesrc=color=white:amplitude=0.3", seconds=4))
    assert white["wind_likely"] is False and white["lf_share_db"] < -8
    rumble, _ = measure(ff, gen(ff, tmp_path / "r.wav", "sine=f=80", filters="volume=-12dB"))
    assert rumble["wind_likely"] is True      # documented limitation: a pure low rumble looks like wind


def test_extract_audio_formats_chunks_and_errors(tmp_path, ff):
    from pathlib import Path

    src = gen(ff, tmp_path / "src.wav", "sine=f=300", seconds=4, fmt="pcm_s16le")
    out = tmp_path / "x" / "a.wav"
    assert au.extract_audio(Path(ff), src, out, "wav") is None
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), round(w.getnframes() / w.getframerate())) == (16000, 1, 4)
    assert not list(out.parent.glob("*.part"))
    chunk = tmp_path / "x" / "b.wav"
    assert au.extract_audio(Path(ff), src, chunk, "wav", start=1.0, duration=2.0) is None
    with wave.open(str(chunk)) as w:
        assert round(w.getnframes() / w.getframerate(), 1) == 2.0
    flac = tmp_path / "x" / "c.flac"
    assert au.extract_audio(Path(ff), src, flac, "flac") is None and flac.read_bytes()[:4] == b"fLaC"
    err = au.extract_audio(Path(ff), tmp_path / "missing.wav", tmp_path / "x" / "d.wav", "wav")
    assert err and "failed" in err and not (tmp_path / "x" / "d.wav").exists()
