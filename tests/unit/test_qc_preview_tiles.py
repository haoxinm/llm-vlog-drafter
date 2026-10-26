"""Preview sheet planning and index (DESIGN 14 steps 4-5; Phase 5a): frames either side of every cut, labelled, indexed, split into sheets."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from trip_vlog.qc import preview as P

FPS = Fraction(30)


def clips(*spans, scene="scene_01"):
    return [P.CutClip(f"clip_{i:03d}", scene if isinstance(scene, str) else scene[i - 1], a, b) for i, (a, b) in enumerate(spans, 1)]


def test_tiles_before_and_after_each_cut_with_ids():
    plan = P.plan_tiles(clips((0, 150), (150, 270), (270, 360)), FPS, 360, offset_seconds=0.15)   # 0.15 s = 4.5 frames, rounded to 4
    t = {x.tile_id: x for x in plan.tiles}
    assert [(x.side, x.record_frame, x.clip_instance_id) for x in plan.tiles] == [
        ("start", 0, "clip_001"), ("before", 146, "clip_001"), ("after", 154, "clip_002"),
        ("before", 266, "clip_002"), ("after", 274, "clip_003"), ("end", 359, "clip_003")]
    assert plan.cuts_total == plan.cuts_sampled == 2
    c1 = plan.cuts[0]
    assert (c1.cut_index, c1.record_frame, c1.record_seconds, c1.outgoing_clip_instance_id, c1.incoming_clip_instance_id) == (1, 150, 5.0, "clip_001", "clip_002")
    assert t[c1.before_tile].record_seconds == round(146 / 30, 4) and t[c1.after_tile].cut_index == 1


def test_offset_is_clamped_inside_short_clips():
    plan = P.plan_tiles(clips((0, 3), (3, 6), (6, 100)), FPS, 100, offset_seconds=0.5)   # 15-frame offset, 3-frame clips
    by = [(x.side, x.record_frame, x.clip_instance_id) for x in plan.tiles]
    assert ("before", 0, "clip_001") in by and ("after", 5, "clip_002") in by and ("before", 3, "clip_002") in by
    for x in plan.tiles:
        c = next(c for c in clips((0, 3), (3, 6), (6, 100)) if c.clip_instance_id == x.clip_instance_id)
        assert c.start_frame <= x.record_frame < c.end_frame


def test_scene_ids_follow_the_clip_each_side():
    cs = clips((0, 60), (60, 120), scene=["scene_01", "scene_02"])
    plan = P.plan_tiles(cs, FPS, 120)
    assert plan.cuts[0].outgoing_scene_id == "scene_01" and plan.cuts[0].incoming_scene_id == "scene_02"
    assert [x.scene_id for x in plan.tiles if x.side in ("before", "after")] == ["scene_01", "scene_02"]


def test_single_clip_has_start_and_end_only():
    plan = P.plan_tiles(clips((0, 90)), FPS, 90)
    assert [x.side for x in plan.tiles] == ["start", "end"] and plan.cuts == []
    assert P.plan_tiles([], FPS, 90).tiles == []


def test_many_cuts_split_into_sheets_without_splitting_a_pair():
    cs = clips(*[(i * 30, i * 30 + 30) for i in range(25)])
    plan = P.plan_tiles(cs, FPS, 750, max_per_sheet=10, max_sheets=20)
    sheets = P.split_sheets(plan.groups, 10)
    assert plan.cuts_total == plan.cuts_sampled == 24 and len(plan.tiles) == 50
    assert all(len(s) <= 10 for s in sheets) and sum(len(s) for s in sheets) == 50
    for s in sheets:   # before/after of a cut are always on the same sheet
        for x in s:
            if x.side == "before":
                assert any(y.side == "after" and y.cut_index == x.cut_index for y in s)


def test_sheet_cap_samples_cuts_evenly_and_records_it():
    cs = clips(*[(i * 30, i * 30 + 30) for i in range(101)])
    plan = P.plan_tiles(cs, FPS, 3030, max_per_sheet=10, max_sheets=2)
    assert plan.cuts_total == 100 and plan.cuts_sampled == 8 and len(P.split_sheets(plan.groups, 10)) == 2
    idx = [c.cut_index for c in plan.cuts]
    assert idx[0] == 1 and idx[-1] == 100 and idx == sorted(idx)       # real cut numbers, first and last kept


def test_sheet_paths_keep_the_first_name():
    base = Path("preview") / "contact_sheet_timeline.jpg"
    assert P.sheet_path_for(base, 1) == base and P.sheet_path_for(base, 2).name == "contact_sheet_timeline_02.jpg"


def test_label_names_time_scene_and_clip():
    plan = P.plan_tiles(clips((0, 150), (150, 270)), FPS, 270)
    after = next(x for x in plan.tiles if x.side == "after")
    l1, l2 = P.tile_label(after)
    assert l1.startswith("cut 1 after") and "00:05.133" in l1 and l2 == "scene_01  clip_002"


def test_index_roundtrip_and_lookup(tmp_path):
    idx = P.PreviewSheetIndex("preview/draft.mp4", "ab" * 32, "30", 270, 4, 320, 0.15, cuts_total=1, cuts_sampled=1)
    idx.sheets.append(P.SheetEntry(1, "preview/contact_sheet_timeline.jpg", ["tile_001", "tile_002"]))
    idx.tiles["tile_001"] = P.TileEntry("tile_001", 1, 0, 0, 0, 0.0, 0, "clip_001", "scene_01", "start", None)
    idx.tiles["tile_002"] = P.TileEntry("tile_002", 1, 1, 0, 1, 4.8667, 146, "clip_001", "scene_01", "before", 1, False, "no frame")
    idx.cuts.append(P.CutInfo(1, 150, 5.0, "clip_001", "clip_002", "scene_01", "scene_01", "tile_002", None))
    path = tmp_path / "qc" / "preview_sheet.json"
    path.parent.mkdir()
    path.write_text(json.dumps(idx.to_dict()), encoding="utf-8")
    back = P.load_preview_sheet_index(path)
    assert back.to_dict() == idx.to_dict() and not back.truncated
    assert [t.tile_id for t in back.tiles_for_clip("clip_001")] == ["tile_001", "tile_002"]
    d = idx.to_dict()["tiles"]["tile_002"]
    assert set(d) >= {"record_seconds", "clip_instance_id", "scene_id", "side", "sheet", "extracted", "error"} and d["record_seconds"] == 4.8667
    with pytest.raises(ValueError):
        P.PreviewSheetIndex.from_dict({"schema_version": 99})


def test_build_sheets_with_a_real_ffmpeg(tmp_path, generated_media, ffmpeg_tools):
    ffmpeg = Path(ffmpeg_tools[0])
    video = generated_media / "landscape_5s.mp4"        # 150 frames at 30 fps
    cs = clips(*[(i * 30, i * 30 + 30) for i in range(5)])
    plan = P.plan_tiles(cs, FPS, 150, max_per_sheet=4, max_sheets=5)
    res = P.build_preview_sheets(ffmpeg, video, tmp_path / "sheet.jpg", plan, FPS, columns=2, tile_width=160, max_per_sheet=4, duration_frames=150,
                                 plan_hash="x" * 64, rel=lambda p: Path(p).name)
    assert not res.failed and res.frames == 10 and [p.name for p in res.written] == ["sheet.jpg", "sheet_02.jpg", "sheet_03.jpg"]
    assert all(p.stat().st_size > 500 for p in res.written)
    tiles = res.index.tiles
    assert tiles["tile_002"].side == "before" and tiles["tile_002"].sheet == 1 and tiles["tile_003"].side == "after"
    assert tiles["tile_004"].sheet == 2 and tiles["tile_004"].row == 0 and tiles["tile_004"].col == 0 and tiles["tile_005"].col == 1
    assert [s.path for s in res.index.sheets] == ["sheet.jpg", "sheet_02.jpg", "sheet_03.jpg"]
    # a later, smaller run removes the sheets it no longer needs
    plan2 = P.plan_tiles(cs[:2], FPS, 60, max_per_sheet=4)
    res2 = P.build_preview_sheets(ffmpeg, video, tmp_path / "sheet.jpg", plan2, FPS, max_per_sheet=4, duration_frames=60)
    assert [p.name for p in res2.written] == ["sheet.jpg"] and not (tmp_path / "sheet_02.jpg").exists()
