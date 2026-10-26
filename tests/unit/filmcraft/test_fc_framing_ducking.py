"""Phase 7b: scale-to-fit framing and Volume keyframes (ducking) in the spec, the safety allowlist, the adapter and the read-back verification."""
from __future__ import annotations

import sys as _sys
from dataclasses import replace
from pathlib import Path as _P

import pytest

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft.errors import CapabilityError, ParameterError
from trip_vlog.filmcraft.safety import PathPolicy, validate_command
from trip_vlog.filmcraft.timeline_spec import GainKey, TimelineSpec, fit_scale_percent, validate_spec


def with_features(spec: TimelineSpec, *, keys=((30, 0.0), (36, -12.0), (90, -12.0), (96, 0.0))) -> TimelineSpec:
    pl = []
    for p in spec.placements:
        if p.clip_instance_id == "c3":
            p = replace(p, fit_source_size=(1080, 1920))
        elif p.clip_instance_id == "c1":
            p = replace(p, fit_source_size=(3840, 2160))
        elif p.clip_instance_id == "c4":
            p = replace(p, gain_keys=tuple(GainKey(f, d) for f, d in keys))
        pl.append(p)
    return replace(spec, placements=tuple(pl))


def test_fit_scale_percent():
    assert fit_scale_percent(1920, 1080, 1080, 1920) == pytest.approx(56.25)
    assert fit_scale_percent(1920, 1080, 3840, 2160) == pytest.approx(50.0)
    assert fit_scale_percent(1080, 1920, 1920, 1080) == pytest.approx(56.25)      # landscape in a portrait sequence


def test_new_fields_do_not_change_the_hash_of_a_spec_that_does_not_use_them(tmp_path):
    media = fcsupport.make_fake_media(tmp_path / "m")
    spec = fcsupport.sample_spec(media)
    assert "fit_source_size" not in spec.placements[0].to_dict() and "gain_keys" not in spec.placements[3].to_dict()
    feat = with_features(spec)
    assert feat.spec_hash() != spec.spec_hash()
    assert TimelineSpec.from_dict(feat.to_dict()) == feat


def test_validation_of_fit_and_keys(tmp_path):
    media = fcsupport.make_fake_media(tmp_path / "m")
    spec = fcsupport.sample_spec(media)
    assert validate_spec(with_features(spec)) == []
    bad = with_features(spec, keys=((0, 0.0), (500, -12.0)))
    assert any("outside the clip" in i for i in validate_spec(bad))
    bad = with_features(spec, keys=((30, 0.0), (30, -12.0)))
    assert any("strictly increasing" in i for i in validate_spec(bad))
    bad = with_features(spec, keys=((30, -200.0),))
    assert any("[-96, 15]" in i for i in validate_spec(bad))
    pl = tuple(replace(p, gain_keys=(GainKey(1, 0.0),)) if p.clip_instance_id == "c1" else p for p in spec.placements)
    assert any("stand-alone audio" in i for i in validate_spec(replace(spec, placements=pl)))
    pl = tuple(replace(p, fit_source_size=(0, 5)) if p.clip_instance_id == "c1" else p for p in spec.placements)
    assert any("fit_source_size" in i for i in validate_spec(replace(spec, placements=pl)))


def test_allowlist_only_admits_volume_level_keyframes_and_muting(tmp_path):
    pol = PathPolicy(tmp_path)
    assert validate_command("effects.setParam", {"clip": 3, "effect": "volume", "param": "level", "value": -12.0, "time": 5, "keyframe": True}, pol)
    for params in ({"clip": 3, "effect": "motion", "param": "scale", "value": 50}, {"clip": 3, "effect": "volume", "param": "level", "value": 99},
                   {"clip": 3, "effect": "volume", "param": "level", "value": -3, "path": "x"}):
        with pytest.raises(ParameterError):
            validate_command("effects.setParam", params, pol)
    assert validate_command("mixer.setStrip", {"strip": "A1", "muted": True}, pol)
    for params in ({"strip": "A1", "muted": False}, {"strip": "Mix", "muted": True}):
        with pytest.raises(ParameterError):
            validate_command("mixer.setStrip", params, pol)
    assert validate_command("clip.fitToFrame", {"clips": [1, 2]}, pol)
    with pytest.raises(ParameterError):
        validate_command("clip.fitToFrame", {"clips": []}, pol)
    assert validate_command("file.exportMedia", {"path": str(tmp_path / "a.wav"), "format": "wav", "wait": True}, pol)


@pytest.fixture(autouse=True)
def uhd_landscape(monkeypatch):
    monkeypatch.setenv("FAKE_FC_SIZES", '{"landscape_5s.mp4": [3840, 2160]}')


def test_build_applies_fit_and_keyframes_and_reads_them_back(fc):
    adapter, media, ws = fc
    spec = with_features(fcsupport.sample_spec(media))
    res = adapter.build_project(spec)
    assert res.verification.ok, res.verification.summary()
    assert res.verification.checked["framing"] == 2 and res.verification.checked["ducking"] == 1
    adapter.open_project(res.project_path)
    seq = adapter.inspect_sequence()
    scales = {c.name: c.scale for c in seq.track("V1").clips + seq.track("V2").clips}
    assert scales["portrait_4s.mp4"] == pytest.approx(56.25) and scales["landscape_5s.mp4"] == pytest.approx(50.0)
    assert scales["still_1080p.png"] == pytest.approx(100.0)       # not asked for
    assert seq.track("A2").clips[0].volume_keyframes == 4


def test_a_scale_or_keyframe_count_that_differs_is_a_verification_mismatch(fc):
    adapter, media, ws = fc
    spec = with_features(fcsupport.sample_spec(media))
    res = adapter.build_project(spec)
    adapter.open_project(res.project_path)
    wrong = replace(spec, placements=tuple(
        replace(p, fit_source_size=(1920, 1080)) if p.clip_instance_id == "c3" else
        replace(p, gain_keys=p.gain_keys[:2]) if p.clip_instance_id == "c4" else p for p in spec.placements))
    rep = adapter.verify(wrong)
    assert {m.kind for m in rep.mismatches} == {"ducking"}           # a wrong scale is a warning (QC framing), a wrong keyframe count fails
    assert {m.kind for m in rep.warnings} == {"framing"}


def test_missing_capability_is_a_clear_error_and_nothing_is_half_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FC_OMIT_COMMAND", "clip.fitToFrame")
    adapter, media, ws = fcsupport.make_adapter(tmp_path)
    try:
        with pytest.raises(CapabilityError, match="fit_to_frame"):
            adapter.build_project(with_features(fcsupport.sample_spec(media)))
        assert not adapter.discover_capabilities().has("fit_to_frame")
    finally:
        adapter.close()
