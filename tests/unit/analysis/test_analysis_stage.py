from __future__ import annotations

import json
import time

import pytest
from analysis_helpers import (Call, LabelingTransport, add_photo, add_video, entry, fake_frames, gateway_for, labeling_config, make_ctx,
                              reply, write_photo)

from trip_vlog.analysis import frames as fr
from trip_vlog.analysis import stage as st
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.llm.types import LLMConfigError, ReplayMissError
from trip_vlog.logging_utils import Status


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    st.gateway_factory = None


def setup(tmp_path, tr=None, cfg=None, photos=2, **media):
    cfg = cfg or labeling_config(**media)
    ctx = make_ctx(tmp_path, cfg)
    aid, segs = add_video(ctx.catalog, "a.mp4", 14.0, gps={"lat": 51.123456, "lon": 7.654321})
    fake_frames(ctx.workspace.root, segs, aid, "fast:a.mp4", fr.SamplingSettings.from_config(cfg, cfg.media.vision_batch_max_images))
    for i in range(photos):
        add_photo(ctx.catalog, f"p{i}.jpg", gps={"lat": 51.123456, "lon": 7.654321})
        write_photo(ctx.paths.media_root / f"p{i}.jpg", pattern=1 if i < 2 else 6)
    tr = tr or LabelingTransport()
    st.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    return ctx, tr, segs


def moments(ctx):
    return {r["moment_id"]: json.loads(r["data_json"]) for r in ctx.catalog.list_moments()}


def test_first_run_labels_segments_and_photos(tmp_path):
    ctx, tr, segs = setup(tmp_path, best=None) if False else setup(tmp_path)
    tr.best = lambda sid, n: {"first_index": 1, "last_index": n - 2}
    res = st.analyze_stage(ctx)
    assert res.status == Status.SUCCEEDED and res.summary["labeled"] == 5 and res.summary["fallback_unlabeled"] == 0
    assert res.summary["requests"] == 2 and [len(c.sids) for c in tr.calls] == [3, 2]            # videos batched, photos batched
    ms = moments(ctx)
    assert len(ms) == 5 and all(m["source"] == "llm" and m["labeled"] for m in ms.values())
    for s in segs:
        m = ms["moment_" + s["segment_id"][4:]]                                                      # shared ID derivation
        assert m["segment_id"] == s["segment_id"] and s["start_seconds"] < m["source_in_seconds"] < m["source_out_seconds"] < s["end_seconds"]
        assert len(m["evidence"]) == 5 and m["evidence"][0]["artifact"].startswith("media_index/analysis/frames/")
    photos = [m for m in ms.values() if m["media_type"] == "image"]
    assert photos and all(m["source_in_seconds"] is None and "best_frames" in m for m in photos)
    lines = (ctx.workspace.moments_jsonl).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert ctx.catalog.get_asset(next(iter(ms.values()))["asset_id"])["analysis_status"] == "complete"


def test_rerun_is_a_pure_cache_hit(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    n = len(tr.calls)
    frames_dir = ctx.workspace.root / "media_index/analysis/frames"
    stamps = {p: p.stat().st_mtime_ns for p in frames_dir.rglob("*.jpg")}
    res = st.analyze_stage(ctx)
    assert len(tr.calls) == n and res.summary["requests"] == 0 and res.summary["already_labeled"] == 5
    assert stamps == {p: p.stat().st_mtime_ns for p in frames_dir.rglob("*.jpg")}
    # even with the per-segment rows gone, the gateway response cache answers: still no transport call
    ctx.catalog.conn.execute("DELETE FROM analysis_cache WHERE analyzer LIKE 'vision_label:%'")
    ctx.catalog.conn.commit()
    res = st.analyze_stage(ctx)
    assert len(tr.calls) == n and res.summary["response_cache_hits"] == 2 and res.summary["labeled"] == 5


def _calls_after(tmp_path, mutate):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    n = len(tr.calls)
    mutate(ctx)
    st.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    st.analyze_stage(ctx)
    return len(tr.calls) - n


def test_invalidation_by_sampling_fps(tmp_path):
    def mutate(ctx):
        ctx.config = labeling_config(vision_sampling_fps=2.0)
        fake_frames(ctx.workspace.root, [json.loads(s["data_json"]) for s in ctx.catalog.list_segments()
                                         if json.loads(s["data_json"])["media_type"] == "video"], "", "fast:a.mp4",
                    fr.SamplingSettings.from_config(ctx.config, 16))
    # videos are re-sampled and re-labeled (frames change); photos keep their labels
    assert _calls_after(tmp_path, mutate) == 2      # 3 segments x 8 frames = 24 images -> 2 requests; photos untouched


def test_sampling_fps_changes_cache_key_and_frame_names():
    from trip_vlog.analysis.vision import VisionSettings

    a = VisionSettings.from_config(labeling_config(vision_sampling_fps=1.0), {})
    b = VisionSettings.from_config(labeling_config(vision_sampling_fps=2.0), {})
    assert a.cache_key("video") != b.cache_key("video") and a.cache_key("photo") == b.cache_key("photo")


def test_invalidation_by_model_prompt_and_input_mode(tmp_path, monkeypatch):
    def model(ctx):
        ctx.config = labeling_config()
        ctx.config.llm.endpoints["main"].model = "other-model"
    assert _calls_after(tmp_path / "m", model) == 2

    def prompt(ctx):
        from trip_vlog.analysis import vision
        from trip_vlog.prompts import PromptTemplate

        real = vision.load_template

        def bumped(tid, version=None, **kw):
            t = real(tid, version, **kw)
            return PromptTemplate(t.id, t.version + 1, t.text, t.sha256) if tid == "segment_labels" else t
        monkeypatch.setattr(vision, "load_template", bumped)
    assert _calls_after(tmp_path / "p", prompt) == 1                       # only the video prompt changed; photos stay cached

    def mode(ctx):
        ctx.config = labeling_config(vision_input="contact_sheet")
    assert _calls_after(tmp_path / "i", mode) == 1


def test_changing_only_story_settings_does_not_reanalyze(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    a = st.analyze_inputs(ctx)
    ctx.config = labeling_config()
    ctx.config.project.target_duration_seconds = 45
    ctx.config.project.style = "calm"
    assert st.analyze_inputs(ctx) == a


def test_changed_content_version_relabels_that_asset_only(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    n = len(tr.calls)
    aid, segs = add_video(ctx.catalog, "a.mp4", 14.0, cv="fast:changed")
    fake_frames(ctx.workspace.root, segs, aid, "fast:changed", fr.SamplingSettings.from_config(ctx.config, 16))
    res = st.analyze_stage(ctx)
    # the catalog label of the changed asset is invalid; identical frames are then answered by the gateway's response cache
    assert res.summary["already_labeled"] == 2 and res.summary["labeled"] == 3 and len(tr.calls) == n
    assert res.summary["response_cache_hits"] == 1


def test_referential_validation_partial_retry_and_fallback(tmp_path):
    ctx, tr, segs = setup(tmp_path, photos=0)
    bad_id, miss_id, ok_id = (s["segment_id"] for s in segs)

    def respond(call: Call, default):
        if not call.retry:
            out = [dict(e) for e in default]
            out[0]["best_frames"] = {"first_index": 3, "last_index": 1}                 # last < first
            out = [e for e in out if e["segment_id"] != miss_id]                         # omitted
            out.append(entry("seg_" + "f" * 16, 5))                                      # unknown id
            return out
        return [entry(bad_id, 5, best_frames={"first_index": 0, "last_index": 99}) if e["segment_id"] == bad_id else e
                for e in default]                                                         # bad one still invalid -> fallback
    tr.respond = respond
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL
    assert res.summary["labeled"] == 2 and res.summary["fallback_unlabeled"] == 1 and res.summary["retried_items"] == 2
    assert res.summary["unknown_ids_ignored"] == 1 and res.summary["rejected_items"] >= 3
    assert [c.retry for c in tr.calls] == [False, True]                                   # one retry round, grouped
    assert sorted(len(c.sids) for c in tr.calls if c.retry) == [2]
    ms = moments(ctx)
    bad = ms["moment_" + bad_id[4:]]
    assert bad["labeled"] is False and bad["source"] == "baseline" and "outside" in bad["label_error"]
    assert ms["moment_" + miss_id[4:]]["labeled"] and ms["moment_" + ok_id[4:]]["labeled"]
    assert any("last_index" in e["error"] or "outside" in e["error"] for e in res.summary["errors"])
    errs = (ctx.workspace.errors_log).read_text(encoding="utf-8")
    assert bad_id in errs
    # the next run retries only the unlabeled segment
    n = len(tr.calls)
    tr.respond = None
    res = st.analyze_stage(ctx)
    assert res.status == Status.SUCCEEDED and len(tr.calls) == n + 1 and tr.calls[-1].sids == [bad_id]


def test_batch_level_schema_failure_retries_singly_then_falls_back(tmp_path):
    ctx, tr, segs = setup(tmp_path, photos=0)
    tr.respond = lambda call, default: HttpResponse(200, {}, json.dumps({"content": [{"type": "text", "text": "not json"}], "stop_reason": "end_turn",
                                                                         "model": "m", "usage": {}}).encode())
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and res.summary["fallback_unlabeled"] == 3
    assert res.summary["requests"] == 4                                                     # 1 batch + 3 single re-asks
    assert sorted({c.sids[0] for c in tr.calls if c.retry}) == sorted(s["segment_id"] for s in segs)
    assert all(m["labeled"] is False for m in moments(ctx).values())


def test_fatal_error_stops_and_keeps_finished_batches(tmp_path):
    ctx, tr, _ = setup(tmp_path, vision_batch_max_images=6)                                  # 5 frames per segment -> 3 video requests + photo
    def respond(call, default):
        return HttpResponse(401, {}, b'{"error":{"message":"bad key"}}') if len(tr.calls) == 2 else default
    tr.respond = respond
    with pytest.raises(Exception, match="authentication failed"):
        st.analyze_stage(ctx)
    ms = moments(ctx)
    assert sum(1 for m in ms.values() if m.get("source") == "llm") == 1                      # first batch persisted
    assert (ctx.workspace.moments_jsonl).is_file()
    tr.respond = None
    n = len(tr.calls)
    res = st.analyze_stage(ctx)
    assert res.summary["already_labeled"] == 1 and res.summary["labeled"] == 4 and len(tr.calls) - n == 3   # resumes where it stopped


def test_missing_frames_without_ffmpeg_falls_back_but_photos_are_labeled(tmp_path):
    ctx, tr, segs = setup(tmp_path)
    for p in (ctx.workspace.root / "media_index/analysis").rglob("*.jpg"):
        p.unlink()
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and res.summary["fallback_unlabeled"] == 3 and res.summary["labeled"] == 2
    assert all("ffmpeg not found" in m["label_error"] for m in moments(ctx).values() if m["media_type"] == "video")


def test_photos_duplicate_groups_and_no_best_frames(tmp_path):
    ctx, tr, _ = setup(tmp_path, photos=3)                                                  # p0,p1 same pattern; p2 different
    st.analyze_stage(ctx)
    ph = sorted((m for m in moments(ctx).values() if m["media_type"] == "image"), key=lambda m: m["asset_id"])
    groups = {m["asset_id"]: m["duplicate_group"] for m in ph}
    from trip_vlog.media.ids import asset_id_for
    g0, g1, g2 = (groups[asset_id_for(f"p{i}.jpg")] for i in range(3))
    assert g0 == g1 and g0 and g2 is None
    photo_calls = [c for c in tr.calls if "photo(s)" in c.texts[0]]
    assert photo_calls and "best_frames" not in json.dumps(photo_calls[0].body["output_config"])


def test_gps_never_in_requests_or_exports(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    blob = json.dumps([c.body for c in tr.calls]) + ctx.workspace.moments_jsonl.read_text(encoding="utf-8")
    assert "51.1234" not in blob and "7.6543" not in blob and "gps" not in blob.lower()
    assert all("source_sha256" not in c.body for c in tr.calls)


def test_filenames_are_untrusted_wrapped_in_requests(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    st.analyze_stage(ctx)
    first = tr.calls[0].texts
    assert any('<untrusted_media_data kind="filename">\na.mp4\n</untrusted_media_data>' in t for t in first)
    assert "untrusted DATA" in first[0]


def test_dry_run_counts_match_a_real_run_and_send_nothing(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    rep = st.dry_run_report(ctx)
    assert tr.calls == []
    assert (rep["video_segments"], rep["photos"], rep["frames"], rep["images"], rep["requests"]) == (3, 2, 15, 17, 2)
    assert rep["where"] == "remote" and rep["endpoint"] == "main" and rep["model"] == "test-model"
    assert not (ctx.workspace.root / "cache").exists()
    text = st.format_dry_run(rep)
    assert "remote" in text and "15" in text and "leave this machine" in text
    st.analyze_stage(ctx)
    assert len(tr.calls) == rep["requests"] and sum(c.images for c in tr.calls) == rep["images"]
    after = st.dry_run_report(ctx)
    assert after["requests"] == 0 and after["video_segments_cached"] == 3 and after["images"] == 0
    assert st.dry_run_report(ctx, limit=1)["assets"] == 1


def test_limit_restricts_assets(tmp_path):
    ctx, tr, _ = setup(tmp_path)
    ctx.data["analyze_limit"] = 1
    res = st.analyze_stage(ctx)
    assert res.summary["assets"] == 1 and res.summary["video_segments"] == 3 and res.summary["photos"] == 0


def test_local_endpoint_reported_and_images_required(tmp_path):
    cfg = labeling_config()
    cfg.llm.endpoints["main"].supports_images = False
    ctx, _, _ = setup(tmp_path, cfg=cfg)
    with pytest.raises(LLMConfigError, match="supports_images"):
        st.analyze_stage(ctx)


def test_fine_sampling_is_reported_as_unsupported(tmp_path):
    ctx, tr, _ = setup(tmp_path, fine_sampling_enabled=True, fine_sampling_fps=4.0)
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and "fine_sampling" in res.summary["warnings"][0]


def test_stale_llm_moments_are_dropped_when_segments_change(tmp_path):
    ctx, tr, segs = setup(tmp_path, photos=0)
    st.analyze_stage(ctx)
    aid = segs[0]["asset_id"]
    from trip_vlog.media.segment import SegmentSettings, build_segments, moments_from_segments

    new = build_segments(aid, "video", 14.0, __import__("fractions").Fraction(30), {"cuts": [], "silences": [], "windows": [], "has_audio": False},
                         SegmentSettings(window_seconds=3.0))
    ctx.catalog.replace_segments(aid, "fast:a.mp4", new)
    ctx.catalog.replace_moments(aid, "fast:a.mp4", moments_from_segments(new, "fast:a.mp4"))
    ms = moments(ctx)
    assert len(ms) == len(new) and all(m["source"] == "baseline" for m in ms.values())
