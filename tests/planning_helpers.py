"""Synthetic catalog + plan builders shared by the planning tests (no ffmpeg or SQLite needed)."""

from __future__ import annotations

from fractions import Fraction

from trip_vlog.media.ids import asset_id_for
from trip_vlog.media.segment import moment_id_for, segment_id_for
from trip_vlog.planning.catalog_view import AssetInfo, MomentInfo, PlanningCatalog
from trip_vlog.planning.models import SelectionClip, SelectionPlan, SelectionScene, SelectionTransition

NTSC = Fraction(30000, 1001)


def aid(path: str) -> str:
    return asset_id_for(path)


def mk_asset(path: str, media_type="video", duration=20.0, fps=Fraction(30), w=1920, h=1080, audio=1, cv=None, capture=None, **kw) -> AssetInfo:
    return AssetInfo(asset_id=aid(path), relative_path=path, media_type=media_type, duration_seconds=duration if media_type != "image" else None,
                     fps=fps if media_type == "video" else None, width=w, height=h, display_width=w, display_height=h,
                     audio_streams=audio, content_version=cv or f"fast:{path}", capture_time=capture, **kw)


def mk_moments(asset: AssetInfo, bounds, silent=False, **feat) -> list[MomentInfo]:
    out = []
    if asset.media_type == "image":
        sid = segment_id_for(asset.asset_id, None, None)
        return [MomentInfo(moment_id_for(sid), asset.asset_id, sid, None, None, content_version=asset.content_version, tags=("image", "still"),
                           features={"still": True})]
    fps = asset.fps or Fraction(1000)
    for a, b in bounds:
        sid = segment_id_for(asset.asset_id, int(a * fps), int(b * fps))
        f = {"audio_mean_db": -30.0, "silent": silent, "scene_cut_score": 0.0, "clipped_audio_hint": False, **feat}
        out.append(MomentInfo(moment_id_for(sid), asset.asset_id, sid, float(a), float(b), content_version=asset.content_version, features=f))
    return out


def make_catalog() -> PlanningCatalog:
    a1 = mk_asset("day1/a.mp4", duration=20.0, capture="2026-10-01T09:00:00+00:00")
    a2 = mk_asset("day1/b.mp4", duration=10.0, fps=NTSC, capture="2026-10-01T09:05:00+00:00")
    a3 = mk_asset("day1/c.jpg", media_type="image", capture="2026-10-01T09:10:00+00:00")
    a4 = mk_asset("day2/p.mp4", duration=8.0, w=1080, h=1920, capture="2026-10-02T10:00:00+00:00")
    a5 = mk_asset("music/song.wav", media_type="audio", duration=30.0, w=None, h=None)
    a6 = mk_asset("day2/silent.mp4", duration=12.0, audio=0, capture="2026-10-02T10:30:00+00:00")
    moments = (mk_moments(a1, [(0, 6), (6, 12), (12, 18)]) + mk_moments(a2, [(0, 5), (5, 10)]) + mk_moments(a3, []) +
               mk_moments(a4, [(0, 4), (4, 8)]) + mk_moments(a5, [(0, 15), (15, 30)]) + mk_moments(a6, [(0, 6), (6, 12)], silent=True))
    return PlanningCatalog([a1, a2, a3, a4, a5, a6], moments)


def mid(cat: PlanningCatalog, path: str, i: int = 0) -> str:
    return cat.moments_for_asset(aid(path))[i].moment_id


def sel_clip(cid: str, moment_id: str, dur=3.0, anchor="center", role="primary_video", audio="natural_sound",
             dissolve: float = 0.0, repeat: str | None = None) -> SelectionClip:
    t = SelectionTransition(type="dissolve", duration_seconds=dissolve) if dissolve else SelectionTransition(type="cut", duration_seconds=0.0)
    return SelectionClip(clip_instance_id=cid, moment_id=moment_id, target_duration_seconds=dur, anchor=anchor, track_role=role,
                         audio_policy=audio, transition_in=t, rationale="test", intentional_repeat_reason=repeat)


def sel_plan(*scenes: list[SelectionClip], budgets=None) -> SelectionPlan:
    out = []
    for i, clips in enumerate(scenes, start=1):
        out.append(SelectionScene(scene_id=f"scene_{i:03d}", title=f"S{i}", purpose="test",
                                  target_duration_seconds=(budgets[i - 1] if budgets else sum(c.target_duration_seconds for c in clips)), clips=clips))
    return SelectionPlan(schema_version="1.0", planner="test", scenes=out, uncertainties=[])
