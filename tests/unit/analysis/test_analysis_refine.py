"""Fine sampling refinement (DESIGN §7.2 step 5): windows, clamping, reply validation, keys, storage and the stage (no ffmpeg)."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from analysis_helpers import LabelingTransport, add_video, entry, fake_frames, gateway_for, labeling_config, make_ctx
from PIL import Image

from trip_vlog.analysis import frames as fr
from trip_vlog.analysis import refine as rf
from trip_vlog.analysis import stage as st
from trip_vlog.analysis.vision import Item, VisionSettings
from trip_vlog.config import ConfigError, parse_config
from trip_vlog.llm.schema import validation_errors
from trip_vlog.llm.transport import HttpResponse
from trip_vlog.llm.types import LLMConfigError
from trip_vlog.logging_utils import Status
from trip_vlog.planning.catalog_view import PlanningCatalog

SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"
F30 = Fraction(30)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    yield
    st.gateway_factory = None


def fake_extract(monkeypatch):
    """Stand-in for the ffmpeg extractor: writes the planned frame files (the real one is covered by the integration test)."""
    calls = []

    def extract(ffmpeg, src, seg, records, root, settings):
        calls.append(seg["segment_id"])
        for r in records:
            p = root / r["artifact"]
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 54), (r["index"] * 20 % 255, 80, 120)).save(p, format="JPEG")
        return None
    monkeypatch.setattr(fr, "extract_segment_frames", extract)
    return calls


# ---------------------------------------------------------------- windows
def test_windows_are_clipped_to_the_segment_and_never_overlap():
    sw, ew = rf.plan_windows(in_f=90, out_f=150, seg_start=60, seg_end=180, total_frames=1000, span_f=30)
    assert (sw.start, sw.end) == (60, 120) and (ew.start, ew.end) == (120, 180)
    sw, ew = rf.plan_windows(in_f=0, out_f=30, seg_start=0, seg_end=30, total_frames=30, span_f=30)       # at the clip's very start and end
    assert (sw.start, sw.end) == (0, 15) and (ew.start, ew.end) == (15, 30)
    sw, ew = rf.plan_windows(in_f=300, out_f=600, seg_start=0, seg_end=900, total_frames=900, span_f=30)  # plenty of room
    assert (sw.start, sw.end) == (270, 330) and (ew.start, ew.end) == (570, 630) and sw.end <= ew.start


def test_windows_never_exceed_the_asset():
    sw, ew = rf.plan_windows(in_f=100, out_f=290, seg_start=0, seg_end=400, total_frames=300, span_f=30)
    assert ew.end == 300 and sw.start == 70
    assert rf.plan_windows(100, 90, 0, 400, 400, 30) is None and rf.plan_windows(100, 200, 0, 400, 400, 0) is None
    assert rf.plan_windows(100, 500, 0, 400, 400, 30) is None            # moment outside the segment/asset: nothing to do


def test_territory_boundaries_match_the_best_frames_rule():
    w = rf.Window("start", 100, 140)
    sampled = [105, 115, 125, 135]
    assert rf.start_boundary(sampled, 0, w) == 100 and rf.start_boundary(sampled, 2, w) == 120
    assert rf.end_boundary(sampled, 3, w) == 140 and rf.end_boundary(sampled, 1, w) == 120


# ---------------------------------------------------------------- reply resolution
def _setup_resolve():
    sw, ew = rf.plan_windows(in_f=300, out_f=600, seg_start=0, seg_end=900, total_frames=900, span_f=30)
    fs = rf.fr.sample_frame_numbers(sw.start, sw.end, F30, 4.0, 8)
    fe = rf.fr.sample_frame_numbers(ew.start, ew.end, F30, 4.0, 8)
    return (sw, ew), (fs, fe)


def resolve(entry_, **kw):
    wins, sampled = _setup_resolve()
    return rf.resolve_reply(entry_, wins, sampled, in_f=300, out_f=600, total_frames=900, fps=F30, **kw)


def test_null_reply_keeps_the_range():
    res, err = resolve({"start_frame_index": None, "end_frame_index": None})
    assert err is None and res == {"in_frame": 300, "out_frame": 600, "applied": False}


def test_indices_become_territory_boundaries_inside_the_windows():
    wins, (fs, fe) = _setup_resolve()
    res, err = resolve({"start_frame_index": 5, "end_frame_index": 2})
    assert err is None and res["applied"]
    assert res["in_frame"] == (fs[4] + fs[5] + 1) // 2 and res["out_frame"] == (fe[2] + fe[3] + 1) // 2
    assert wins[0].start <= res["in_frame"] <= wins[0].end and wins[1].start <= res["out_frame"] <= wins[1].end
    res, _ = resolve({"start_frame_index": 0, "end_frame_index": 7})          # window edges: widest allowed, never beyond the window
    assert res["in_frame"] == 270 and res["out_frame"] == 630


@pytest.mark.parametrize("bad", [
    {"start_frame_index": 8, "end_frame_index": None}, {"start_frame_index": None, "end_frame_index": -1},
    {"start_frame_index": True, "end_frame_index": None}, {"start_frame_index": 1.5, "end_frame_index": None},
    {"start_frame_index": "2", "end_frame_index": None}])
def test_invalid_indices_are_rejected(bad):
    res, err = resolve(bad)
    assert res is None and err


def test_too_short_result_is_a_valid_no_better_boundary_answer():
    sw, ew = rf.plan_windows(in_f=300, out_f=345, seg_start=0, seg_end=900, total_frames=900, span_f=30)
    fs = rf.fr.sample_frame_numbers(sw.start, sw.end, F30, 4.0, 8)
    fe = rf.fr.sample_frame_numbers(ew.start, ew.end, F30, 4.0, 8)
    res, err = rf.resolve_reply({"start_frame_index": len(fs) - 1, "end_frame_index": 0}, (sw, ew), (fs, fe), in_f=300, out_f=345, total_frames=900, fps=F30)
    assert err is None and res["applied"] is False and (res["in_frame"], res["out_frame"]) == (300, 345) and "less than" in res["reason"]


def test_result_is_clamped_to_the_asset_duration():
    sw, ew = rf.plan_windows(in_f=100, out_f=290, seg_start=0, seg_end=400, total_frames=295, span_f=30)    # asset ends at frame 295
    fe = rf.fr.sample_frame_numbers(ew.start, ew.end, F30, 4.0, 8)
    fs = rf.fr.sample_frame_numbers(sw.start, sw.end, F30, 4.0, 8)
    res, err = rf.resolve_reply({"start_frame_index": None, "end_frame_index": len(fe) - 1}, (sw, ew), (fs, fe), in_f=100, out_f=290, total_frames=295, fps=F30)
    assert err is None and res["out_frame"] == 295


# ---------------------------------------------------------------- schema, prompt, keys
def test_committed_schema_matches_code_and_is_strict():
    assert json.loads((SCHEMAS / "boundary_refine_response.schema.json").read_text(encoding="utf-8")) == rf.refine_schema()
    sch = rf.refine_schema()
    good = {"moments": [{"moment_id": "moment_" + "a" * 16, "start_frame_index": 2, "end_frame_index": None}]}
    assert validation_errors(good, sch) == []
    for bad in ({"moments": [{"moment_id": "m", "start_frame_index": -1, "end_frame_index": None}]},
                {"moments": [{"moment_id": "m", "start_frame_index": 1}]},
                {"moments": [{"moment_id": "m", "start_frame_index": 1, "end_frame_index": 1, "start_seconds": 1.0}]},
                {"moments": [{"moment_id": "m", "start_frame_index": "1", "end_frame_index": None}]}):
        assert validation_errors(bad, sch), bad
    assert "seconds" not in json.dumps(sch["properties"]["moments"]["items"]["properties"]).replace("seconds anywhere", "")


def settings(**media):
    cfg = labeling_config(fine_sampling_enabled=True, **media)
    return rf.RefineSettings.from_config(cfg, VisionSettings.from_config(cfg))


def test_prompt_has_front_matter_version_and_is_part_of_the_key():
    s = settings()
    assert (s.prompt.id, s.prompt.version) == ("boundary_refine", 1) and s.prompt_info() == {"id": "boundary_refine", "version": 1}
    assert "seconds" in s.prompt.text and "null" in s.prompt.text


def test_item_key_covers_every_input_that_changes_the_answer():
    base = settings().item_key(1.0, 4.0)
    assert settings().item_key(1.0, 4.0) == base
    assert settings().item_key(1.0, 4.5) != base and settings().item_key(1.5, 4.0) != base
    for change in ({"fine_sampling_fps": 6.0}, {"fine_sampling_window_seconds": 1.5}, {"vision_input": "contact_sheet"},
                   {"vision_batch_max_images": 8}):
        assert settings(**change).item_key(1.0, 4.0) != base, change
    other_model = labeling_config(fine_sampling_enabled=True)
    other_model.llm.endpoints["main"].model = "another-model"
    assert rf.RefineSettings.from_config(other_model, VisionSettings.from_config(other_model)).item_key(1.0, 4.0) != base
    # selection settings do not invalidate finished refinements, but they do re-run the stage
    assert settings(fine_sampling_min_score=0.9).item_key(1.0, 4.0) == base
    assert settings(fine_sampling_min_score=0.9).stage_key() != settings().stage_key()
    assert labeling_config().media.fine_sampling_enabled is False
    cfg = labeling_config()
    assert rf.RefineSettings.from_config(cfg, VisionSettings.from_config(cfg)).stage_key() is None


def test_config_validation():
    for bad in ({"fine_sampling_min_score": 1.5}, {"fine_sampling_window_seconds": 0}, {"fine_sampling_window_seconds": 11},
                {"fine_sampling_max_per_asset": 0}, {"fine_sampling_max_moments": 0}):
        with pytest.raises(ConfigError):
            parse_config({"media": bad})
    m = parse_config({}).media
    assert (m.fine_sampling_min_score, m.fine_sampling_window_seconds, m.fine_sampling_max_per_asset, m.fine_sampling_max_moments) == (0.7, 1.0, 3, 30)


def test_select_applies_threshold_and_caps_deterministically():
    s = settings(fine_sampling_min_score=0.5, fine_sampling_max_per_asset=2, fine_sampling_max_moments=3)

    def item(aid, sid, interest):
        return (Item("video", {"asset_id": aid}, {"segment_id": sid}, {}, Path("x"), "k", label={"confidence": 0.5}), interest)
    items = [item("a", "seg_1", 0.9), item("a", "seg_2", 0.8), item("a", "seg_3", 0.95), item("b", "seg_4", 0.7), item("b", "seg_5", 0.4),
             item("c", "seg_6", 0.85)]
    chosen = [i.sid for i in rf.select(items, s)]
    assert chosen == ["seg_3", "seg_1", "seg_6"]            # 0.4 below threshold; seg_2 over the per-asset cap; global cap 3
    assert chosen == [i.sid for i in rf.select(list(reversed(items)), s)]


# ---------------------------------------------------------------- storage
def test_apply_refinement_keeps_the_original_and_uses_the_refined_range():
    m = {"source_in_seconds": 1.0, "source_out_seconds": 4.0}
    rec = {"version": 1, "applied": True, "original_in_seconds": 1.0, "original_out_seconds": 4.0, "refined_in_seconds": 1.5, "refined_out_seconds": 3.5}
    out = rf.apply_refinement(dict(m), rec)
    assert (out["source_in_seconds"], out["source_out_seconds"]) == (1.5, 3.5)
    assert out["refinement"]["original_in_seconds"] == 1.0 and out["refinement"]["version"] == rf.REFINE_VERSION
    same = rf.apply_refinement(dict(m), {**rec, "applied": False})
    assert (same["source_in_seconds"], same["source_out_seconds"]) == (1.0, 4.0) and same["refinement"]["applied"] is False
    assert rf.apply_refinement(dict(m), None) == m


# ---------------------------------------------------------------- stage
def stage_setup(tmp_path, monkeypatch, *, enabled=True, high=True, **media):
    media.setdefault("vision_batch_max_images", 100)
    cfg = labeling_config(fine_sampling_enabled=enabled, **media)
    ctx = make_ctx(tmp_path, cfg)
    aid, segs = add_video(ctx.catalog, "a.mp4", 14.0)
    fake_frames(ctx.workspace.root, segs, aid, "fast:a.mp4", fr.SamplingSettings.from_config(cfg, cfg.media.vision_batch_max_images))
    tr = LabelingTransport()
    tr.respond = lambda call, default: [{**e, "interest": 0.9 if high else 0.2} for e in default]
    st.gateway_factory = lambda c: gateway_for(c.config, c.workspace.root, tr)
    extract = fake_extract(monkeypatch)
    return ctx, tr, segs, extract


def moments(ctx):
    return {r["moment_id"]: json.loads(r["data_json"]) for r in ctx.catalog.list_moments()}


def refine_answer(start=1, end=1):
    return lambda call, default: [{**d, "start_frame_index": start, "end_frame_index": end} for d in default]


def test_refinement_is_requested_applied_and_stored_beside_the_original(tmp_path, monkeypatch):
    ctx, tr, segs, extract = stage_setup(tmp_path, monkeypatch)
    tr.refine = refine_answer()
    res = st.analyze_stage(ctx)
    assert res.status == Status.SUCCEEDED and "warnings" not in res.summary
    fine = res.summary["fine_sampling"]
    assert fine["candidates"] == 3 and fine["refined"] == 3 and fine["requests"] == 1 and len(tr.refine_calls) == 1
    call = tr.refine_calls[0]
    assert call.images == sum(len(f) for w in call.frames.values() for f in w.values()) == 24       # 1 s of room per side (moments span whole segments) at 4 fps
    assert all(set(w) == {"start", "end"} for w in call.frames.values())
    ms = moments(ctx)
    for s in segs:
        m = ms["moment_" + s["segment_id"][4:]]
        r = m["refinement"]
        assert r["applied"] and r["original_in_seconds"] == s["start_seconds"] and r["original_out_seconds"] == s["end_seconds"]
        assert (m["source_in_seconds"], m["source_out_seconds"]) == (r["refined_in_seconds"], r["refined_out_seconds"])
        assert s["start_seconds"] <= m["source_in_seconds"] < m["source_out_seconds"] <= s["end_seconds"]       # inside the segment
        assert r["start_frame_index"] == 1 and r["end_frame_index"] == 1 and len(r["windows"]["start"]["frames"]) <= 8
        assert all((ctx.workspace.root / f["artifact"]).is_file() and f["artifact"].startswith("media_index/analysis/fine/")
                   for w in r["windows"].values() for f in w["frames"])
        assert abs(r["refined_in_seconds"] - s["start_seconds"]) <= 1.0 + 1e-6 and abs(r["refined_out_seconds"] - s["end_seconds"]) <= 1.0 + 1e-6
    # the moment table's columns (what planning reads) carry the refined range
    row = ctx.catalog.get_moment("moment_" + segs[0]["segment_id"][4:])
    assert row["source_in_seconds"] == ms[row["moment_id"]]["source_in_seconds"] != segs[0]["start_seconds"]


def test_idempotent_rerun_sends_and_extracts_nothing(tmp_path, monkeypatch):
    ctx, tr, segs, extract = stage_setup(tmp_path, monkeypatch)
    tr.refine = refine_answer()
    st.analyze_stage(ctx)
    before, n_label, n_extract, snap = len(tr.refine_calls), len(tr.calls), len(extract), moments(ctx)
    res = st.analyze_stage(ctx)
    assert len(tr.refine_calls) == before and len(tr.calls) == n_label and len(extract) == n_extract
    assert res.summary["fine_sampling"]["from_cache"] == 3 and res.summary["fine_sampling"]["requests"] == 0
    assert moments(ctx) == snap
    # with the per-moment rows gone the gateway's response cache still answers: no transport call
    ctx.catalog.conn.execute("DELETE FROM analysis_cache WHERE analyzer LIKE 'vision_refine:%'")
    ctx.catalog.conn.commit()
    res = st.analyze_stage(ctx)
    assert len(tr.refine_calls) == before and res.summary["fine_sampling"]["response_cache_hits"] == 1 and moments(ctx) == snap


def test_null_reply_is_cached_as_unchanged_and_keeps_the_range(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    res = st.analyze_stage(ctx)
    assert res.summary["fine_sampling"]["unchanged"] == 3 and res.summary["fine_sampling"]["refined"] == 0
    for s in segs:
        m = moments(ctx)["moment_" + s["segment_id"][4:]]
        assert m["refinement"]["applied"] is False and (m["source_in_seconds"], m["source_out_seconds"]) == (s["start_seconds"], s["end_seconds"])
    st.analyze_stage(ctx)
    assert len(tr.refine_calls) == 1


def test_invalid_reply_keeps_the_original_is_not_cached_and_is_asked_again(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    tr.refine = refine_answer(start=99)                       # schema-valid, outside the 8 frames shown
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and res.summary["fine_sampling"]["rejected"] == 3 and "fine sampling" in res.summary["warnings"][0]
    assert all("refinement" not in m for m in moments(ctx).values())
    assert all((m["source_in_seconds"], m["source_out_seconds"]) in {(s["start_seconds"], s["end_seconds"]) for s in segs} for m in moments(ctx).values())
    assert ctx.catalog.conn.execute("SELECT COUNT(*) FROM analysis_cache WHERE analyzer LIKE 'vision_refine:%'").fetchone()[0] == 0
    assert list((ctx.workspace.root / "cache" / "llm").glob("*.json")) and True
    tr.refine = refine_answer()
    res = st.analyze_stage(ctx)                                # same request -> the bad reply was evicted, so the endpoint is asked again
    assert res.status == Status.SUCCEEDED and len(tr.refine_calls) == 2 and res.summary["fine_sampling"]["refined"] == 3


def test_missing_moment_in_reply_keeps_only_that_one_original(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    tr.refine = lambda call, default: [{**d, "start_frame_index": 1, "end_frame_index": 1} for d in default[:-1]]
    res = st.analyze_stage(ctx)
    fine = res.summary["fine_sampling"]
    assert res.status == Status.PARTIAL and fine["refined"] == 2 and fine["rejected"] == 1 and fine["failed"] == 0
    assert sum("refinement" in m for m in moments(ctx).values()) == 2


def test_only_high_scoring_moments_within_the_caps_are_refined(tmp_path, monkeypatch):
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch, high=False)
    res = st.analyze_stage(ctx)
    assert res.summary["fine_sampling"]["candidates"] == 0 and tr.refine_calls == []
    ctx2, tr2, _, _ = stage_setup(tmp_path / "b", monkeypatch, fine_sampling_max_per_asset=2)
    assert st.analyze_stage(ctx2).summary["fine_sampling"]["candidates"] == 2
    ctx3, tr3, _, _ = stage_setup(tmp_path / "c", monkeypatch, fine_sampling_max_moments=1)
    assert st.analyze_stage(ctx3).summary["fine_sampling"]["candidates"] == 1


def test_disabled_does_nothing_and_turning_it_off_restores_the_labeled_range(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch, enabled=False)
    res = st.analyze_stage(ctx)
    assert tr.refine_calls == [] and "fine_sampling" not in res.summary and res.status == Status.SUCCEEDED
    labeled = moments(ctx)
    ctx.config.media.fine_sampling_enabled = True
    tr.refine = refine_answer()
    st.analyze_stage(ctx)
    assert any("refinement" in m for m in moments(ctx).values())
    ctx.config.media.fine_sampling_enabled = False
    st.analyze_stage(ctx)
    assert moments(ctx) == labeled                                # original ranges back, record gone
    n_rows = lambda: ctx.catalog.conn.execute("SELECT COUNT(*) FROM analysis_cache WHERE analyzer LIKE 'vision_refine:%'").fetchone()[0]
    assert n_rows() == 3                                          # rows are kept while off: toggling back on re-pays nothing
    calls = len(tr.refine_calls)
    ctx.config.media.fine_sampling_enabled = True
    res = st.analyze_stage(ctx)
    assert len(tr.refine_calls) == calls and res.summary["fine_sampling"]["from_cache"] == 3 and any("refinement" in m for m in moments(ctx).values())


def test_changing_the_fine_settings_re_asks_and_prunes_the_old_rows(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    tr.refine = refine_answer()
    st.analyze_stage(ctx)
    ctx.config.media.fine_sampling_window_seconds = 0.5
    st.analyze_stage(ctx)
    assert len(tr.refine_calls) == 2
    assert ctx.catalog.conn.execute("SELECT COUNT(*) FROM analysis_cache WHERE analyzer LIKE 'vision_refine:%'").fetchone()[0] == 3


def _four_hundred(msg):
    return lambda call, default: HttpResponse(400, {}, json.dumps({"error": {"message": msg}}).encode())


def test_breaker_aborts_after_three_400s_when_nothing_has_succeeded(tmp_path, monkeypatch):
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch, enabled=False, vision_batch_max_images=2)
    st.analyze_stage(ctx)                                     # labels come from the cache from here on: no accepted request yet
    ctx.config.media.fine_sampling_enabled = True
    tr.refine = _four_hundred("Unsupported parameter: 'foo'")
    with pytest.raises(LLMConfigError, match="Unsupported parameter"):
        st.analyze_stage(ctx)
    assert len(tr.refine_calls) == 3
    assert all("refinement" not in m for m in moments(ctx).values())


def test_a_400_after_a_success_is_recoverable(tmp_path, monkeypatch):
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch, vision_batch_max_images=2)
    bad = _four_hundred("image rejected")
    tr.refine = lambda call, default: bad(call, default) if len(tr.refine_calls) >= 2 else refine_answer(0, 0)(call, default)
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and res.summary["fine_sampling"]["unchanged"] == 1 and len(tr.refine_calls) == 3


def test_extraction_failure_keeps_the_original_and_is_reported(tmp_path, monkeypatch):
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch)
    real = fr.extract_segment_frames
    monkeypatch.setattr(fr, "extract_segment_frames", lambda f, src, seg, *a, **k: "ffmpeg failed: boom" if seg["segment_id"].startswith("moment_") else real(f, src, seg, *a, **k))
    res = st.analyze_stage(ctx)
    assert res.status == Status.PARTIAL and tr.refine_calls == [] and res.summary["fine_sampling"]["failed"] == 3 and res.summary["fine_sampling"]["rejected"] == 0


def test_endpoint_without_images_is_refused(tmp_path, monkeypatch):
    cfg = labeling_config(fine_sampling_enabled=True)
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch, enabled=False)
    st.analyze_stage(ctx)
    ctx.config.media.fine_sampling_enabled = True
    ctx.config.llm.endpoints["main"].supports_images = False
    with pytest.raises(LLMConfigError, match="supports_images"):
        st.analyze_stage(ctx)


def test_stage_inputs_change_with_the_fine_settings(tmp_path, monkeypatch):
    ctx, _, _, _ = stage_setup(tmp_path, monkeypatch, enabled=False)
    off = st.analyze_inputs(ctx)
    ctx.config.media.fine_sampling_enabled = True
    on = st.analyze_inputs(ctx)
    assert on != off and on["fine"] and off["fine"] is None
    ctx.config.media.fine_sampling_min_score = 0.95
    assert st.analyze_inputs(ctx) != on


def test_dry_run_mentions_fine_sampling(tmp_path, monkeypatch):
    ctx, _, _, _ = stage_setup(tmp_path, monkeypatch)
    rep = st.dry_run_report(ctx)
    assert rep["fine_sampling"]["window_seconds"] == 1.0 and "fine sampling" in st.format_dry_run(rep)
    ctx.config.media.fine_sampling_enabled = False
    assert st.dry_run_report(ctx)["fine_sampling"] is None and "fine sampling" not in st.format_dry_run(st.dry_run_report(ctx))


# ---------------------------------------------------------------- downstream
def test_planning_view_sees_refined_ranges_and_its_fingerprint_changes(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    st.analyze_stage(ctx)                                     # null replies: ranges as labeled
    before = PlanningCatalog.from_catalog(ctx.catalog)
    tr.refine = refine_answer()
    ctx.config.media.fine_sampling_window_seconds = 0.9       # new question -> new answer
    st.analyze_stage(ctx)
    after = PlanningCatalog.from_catalog(ctx.catalog)
    mid = "moment_" + segs[0]["segment_id"][4:]
    assert after.moments[mid].source_in_seconds != before.moments[mid].source_in_seconds
    assert after.moments[mid].length_seconds < before.moments[mid].length_seconds
    assert after.fingerprint() != before.fingerprint()
    from trip_vlog.planning.pipeline import plan_inputs
    assert plan_inputs(ctx)["catalog"] == after.fingerprint()


def test_too_short_answer_is_cached_not_evicted_and_not_asked_again(tmp_path, monkeypatch):
    ctx, tr, segs, _ = stage_setup(tmp_path, monkeypatch)
    tr.refine = refine_answer()
    monkeypatch.setattr(rf, "MIN_REFINED_SECONDS", 50.0)      # any range is "too short"
    res = st.analyze_stage(ctx)
    fine = res.summary["fine_sampling"]
    assert res.status == Status.SUCCEEDED and fine["unchanged"] == 3 and fine["rejected"] == 0
    m = next(iter(moments(ctx).values()))
    assert m["refinement"]["applied"] is False and "less than" in m["refinement"]["reason"]
    assert list((ctx.workspace.root / "cache" / "llm").glob("*.json"))      # the reply stays cached
    n = len(tr.refine_calls)
    st.analyze_stage(ctx)
    assert len(tr.refine_calls) == n


def test_moments_without_room_for_windows_are_counted_as_skipped(tmp_path, monkeypatch):
    ctx, tr, _, _ = stage_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rf, "plan_windows", lambda *a, **k: None)
    res = st.analyze_stage(ctx)
    fine = res.summary["fine_sampling"]
    assert fine["skipped"] == 3 and fine["skipped_reasons"] == {"no room for the boundary windows": 3} and tr.refine_calls == []
    assert res.status == Status.SUCCEEDED
