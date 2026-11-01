from __future__ import annotations

from types import SimpleNamespace

import pytest

from trip_vlog.cli import build_parser, _overrides
from trip_vlog.config import ConfigError, parse_config
from trip_vlog.draft_stages import build_draft_runner
from trip_vlog.logging_utils import RunLogger
from trip_vlog.media.catalog import Catalog
from trip_vlog.media.segment import SegmentSettings, build_segments, moments_from_segments
from trip_vlog.state import Manifest

from analysis_helpers import F30, add_video


def test_config_defaults_and_bounds():
    m = parse_config({}).media
    assert (m.vision_sampling_fps, m.vision_max_frames_per_segment, m.vision_input, m.vision_batch_max_images) == (1.0, 8, "frames", 16)
    for bad in ({"vision_sampling_fps": 0}, {"vision_sampling_fps": 31}, {"vision_input": "grid"}, {"vision_max_frames_per_segment": 0}):
        with pytest.raises(ConfigError):
            parse_config({"media": bad})


def test_sampling_fps_flag_maps_to_config_and_is_validated():
    args = build_parser().parse_args(["analyze", "--output", "w", "--sampling-fps", "2.5"])
    assert _overrides(args)["media.vision_sampling_fps"] == 2.5
    assert parse_config({}, _overrides(args)).media.vision_sampling_fps == 2.5
    draft = build_parser().parse_args(["draft", "--media", "m", "--output", "w", "--sampling-fps", "0.5"])
    assert _overrides(draft)["media.vision_sampling_fps"] == 0.5
    for v in ("0", "-1", "30.5"):
        args = build_parser().parse_args(["analyze", "--output", "w", "--sampling-fps", v])
        with pytest.raises(ConfigError):
            parse_config({}, _overrides(args))


def test_replace_moments_drops_vanished_llm_moments_and_stale_content(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as cat:
        aid, segs = add_video(cat, "a.mp4", 12.0, window=6.0)
        base = moments_from_segments(segs, "fast:a.mp4")
        cat.upsert_moments(aid, "fast:a.mp4", [dict(base[0], source="llm", description="x"), dict(base[1], source="llm", description="y")])
        cat.replace_moments(aid, "fast:a.mp4", base)                      # same content: labels survive
        assert {r["moment_id"] for r in cat.list_moments(aid)} == {b["moment_id"] for b in base}
        assert all(__import__("json").loads(r["data_json"])["source"] == "llm" for r in cat.list_moments(aid))
        cat.replace_moments(aid, "fast:other", base[:1])                  # content changed: label replaced, vanished one dropped
        left = cat.list_moments(aid)
        assert len(left) == 1 and __import__("json").loads(left[0]["data_json"])["source"] == "baseline"


def test_prune_cache(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as cat:
        for key in ("k1", "k2"):
            cat.cache_put("a", "vision_label:s1", key, "cv", {"x": 1})
        cat.cache_put("a", "probe", "k1", "cv", {})
        assert cat.prune_cache("a", "vision_label:", {"k2"}) == 1
        assert cat.cache_get("a", "vision_label:s1", "k2", "cv") and cat.cache_get("a", "probe", "k1", "cv") is not None


def test_analyze_hooked_into_draft_only_for_llm_planner(tmp_path):
    def names(planner):
        cfg = parse_config({"workflow": {"planner": planner}})
        ws = tmp_path / planner
        ctx = SimpleNamespace(config=cfg, manifest=Manifest(ws / "m.json"), logger=RunLogger(ws / "r.jsonl", ws / "e.jsonl", "r"), data={})
        return build_draft_runner(ctx).names
    base, llm = names("baseline"), names("llm")
    assert "analyze" not in base
    assert llm.index("segment") < llm.index("analyze") < llm.index("plan")
