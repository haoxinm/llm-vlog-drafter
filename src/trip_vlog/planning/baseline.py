"""No-LLM baseline planner (DESIGN §20 Phase 2): produces a *selection plan*; the resolver computes exact ranges.

Rules, all deterministic for the same catalog + settings:
  * Order: capture_time (assets without one come last), then relative path; moments by source time.
  * Scenes: a new scene starts when the folder changes, the capture date changes, or the gap between consecutive assets'
    capture times exceeds `scene_gap_minutes`.
  * Skipped: audio-only assets (music/narration are never auto-placed), assets that are missing/unreadable, excluded
    items, video segments shorter than `min_usable_seconds`, and "silent dropouts" (a silent segment in a clip whose other
    segments mostly have sound). Wholly silent clips are kept (muted). must_include items bypass these skips.
  * Similar shots: at most `max_similar_shots_per_scene` per run of consecutive segments of one video, or per burst of
    stills captured within `burst_seconds` of each other.
  * Durations: video clips 3-5 s (shorter only if the segment is), stills `still_duration_seconds`.
  * Budget: candidates are ranked per scene by a simple audio/cut interest score, then taken round-robin across scenes
    until the target duration is reached; the first and last candidates (opening/closing) are taken first, must_include
    first of all. Hard cuts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trip_vlog.planning.catalog_view import AssetInfo, MomentInfo, PlanningCatalog, capture_sort_key, ref_matches, ref_targets
from trip_vlog.planning.models import (SCHEMA_VERSION, SelectionClip, SelectionPlan, SelectionScene, SelectionTransition)

BASELINE_VERSION = 1
BUDGET_SLACK_SECONDS = 1.0


class BaselinePlanError(ValueError):
    """The baseline planner cannot produce a plan (e.g. nothing usable in the catalog)."""


@dataclass(frozen=True)
class BaselineSettings:
    target_duration_seconds: float = 180.0
    clip_min_seconds: float = 3.0
    clip_max_seconds: float = 5.0
    min_usable_seconds: float = 1.0
    still_duration_seconds: float = 3.0
    max_similar_shots_per_scene: int = 2
    opening_hook_seconds: float = 8.0
    closing_seconds: float = 10.0
    scene_gap_minutes: float = 30.0
    burst_seconds: float = 10.0
    preserve_natural_audio: bool = True
    must_include: tuple[str, ...] = ()
    must_exclude: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "BaselineSettings":
        p, e = config.project, config.editorial
        base: dict[str, Any] = dict(
            target_duration_seconds=float(p.target_duration_seconds), max_similar_shots_per_scene=e.max_similar_shots_per_scene,
            opening_hook_seconds=float(e.opening_hook_seconds), closing_seconds=float(e.closing_seconds),
            preserve_natural_audio=p.preserve_natural_audio, must_include=tuple(e.must_include), must_exclude=tuple(e.must_exclude))
        for name in ("still_duration_seconds", "clip_min_seconds", "clip_max_seconds", "scene_gap_minutes"):  # optional config fields
            for section in (config.media, config.project, config.editorial):
                if hasattr(section, name):
                    base[name] = getattr(section, name)
        base.update(overrides)
        return cls(**base)

    def key(self) -> dict[str, Any]:
        return {"v": BASELINE_VERSION, **{k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}}


@dataclass
class _Cand:
    moment: MomentInfo
    asset: AssetInfo
    order: int
    scene: int = 0
    interest: float = 0.0
    duration: float = 0.0
    must: bool = False
    rank: int = 0
    tier: int = 2
    index_in_asset: int = 0
    count_in_asset: int = 1


def interest_score(m: MomentInfo, asset: AssetInfo) -> float:
    """Cheap deterministic interest in [0, 1]: louder, cut-started, unclipped video segments score higher; stills 0.5."""
    if asset.media_type == "image":
        return 0.5
    f = m.features
    mean_db = f.get("audio_mean_db")
    loud = 0.3 if mean_db is None else min(1.0, max(0.0, (float(mean_db) + 60.0) / 60.0))
    cut = min(1.0, float(f.get("scene_cut_score") or 0.0) * 2)
    clean = 0.0 if f.get("clipped_audio_hint") else 1.0
    return round(0.6 * loud + 0.2 * cut + 0.2 * clean, 4)


def _is_dropout(c: _Cand, siblings: list[MomentInfo]) -> bool:
    if not c.asset.has_audio or c.moment.features.get("silent") is not True or len(siblings) < 2:
        return False
    silent = sum(1 for s in siblings if s.features.get("silent") is True)
    return silent * 2 < len(siblings)


def _scene_breaks(assets: list[AssetInfo], s: BaselineSettings) -> list[int]:
    """Scene index for each asset in order."""
    out: list[int] = []
    scene = 0
    prev: AssetInfo | None = None
    for a in assets:
        if prev is not None:
            new = a.folder != prev.folder
            ka, kp = capture_sort_key(a.capture_time), capture_sort_key(prev.capture_time)
            if not new and ka[0] == 0 and kp[0] == 0:
                new = a.capture_time[:10] != prev.capture_time[:10] or (ka[1] - kp[1]) > s.scene_gap_minutes * 60  # type: ignore[index]
            scene += 1 if new else 0
        out.append(scene)
        prev = a
    return out


def _similar(prev: _Cand, cur: _Cand, s: BaselineSettings) -> bool:
    if prev.scene != cur.scene:
        return False
    if prev.asset.asset_id == cur.asset.asset_id:
        return True
    if prev.asset.media_type == "image" and cur.asset.media_type == "image":
        kp, kc = capture_sort_key(prev.asset.capture_time), capture_sort_key(cur.asset.capture_time)
        return kp[0] == 0 and kc[0] == 0 and 0 <= kc[1] - kp[1] <= s.burst_seconds
    return False


def build_baseline_selection(catalog: PlanningCatalog, settings: BaselineSettings) -> SelectionPlan:
    notes: list[str] = []
    usable_assets = [a for a in catalog.assets.values() if a.present and a.probe_ok and a.media_type in ("video", "image")]
    skipped_audio = sum(1 for a in catalog.assets.values() if a.present and a.probe_ok and a.media_type == "audio")
    if skipped_audio:
        notes.append(f"{skipped_audio} audio-only file(s) were not placed: the baseline planner never adds music or narration automatically")
    if not usable_assets:
        raise BaselinePlanError("no usable video or image assets in the catalog (run indexing and segmentation first)")
    usable_assets.sort(key=lambda a: (capture_sort_key(a.capture_time), a.relative_path))
    if any(capture_sort_key(a.capture_time)[0] == 1 for a in usable_assets):
        notes.append("some assets have no capture time; they are ordered after dated assets, by relative path")
    scene_of = dict(zip((a.asset_id for a in usable_assets), _scene_breaks(usable_assets, settings)))

    # candidates (chronological), exclusion, short/dropout skips
    excluded_ids: set[str] = set()
    for ref in settings.must_exclude:
        excluded_ids |= {m.moment_id for m in ref_targets(ref, catalog)}
    must_ids = _must_include_ids(catalog, settings, usable_assets, excluded_ids, notes)
    cands: list[_Cand] = []
    for a in usable_assets:
        moms = catalog.moments_for_asset(a.asset_id)
        for i, m in enumerate(moms):
            if m.moment_id in excluded_ids:
                continue
            c = _Cand(m, a, len(cands), scene_of[a.asset_id], interest_score(m, a), must=m.moment_id in must_ids,
                      index_in_asset=i, count_in_asset=len(moms))
            if a.media_type == "video":
                length = m.length_seconds
                if length is None or length <= 0:
                    continue
                if not c.must and (length < settings.min_usable_seconds or _is_dropout(c, moms)):
                    continue
                c.duration = length
            else:
                c.duration = settings.still_duration_seconds
            cands.append(c)
    if not cands:
        raise BaselinePlanError("no usable moments after applying exclusions and filters")

    _assign_durations(cands, settings)
    cands = _cap_similar(cands, settings)
    chosen = _choose(cands, settings, notes)
    return _to_selection(chosen, settings, notes)


def _must_include_ids(catalog: PlanningCatalog, s: BaselineSettings, assets: list[AssetInfo], excluded: set[str], notes: list[str]) -> set[str]:
    ids: set[str] = set()
    ok_assets = {a.asset_id for a in assets}
    for ref in s.must_include:
        targets = [m for m in ref_targets(ref, catalog) if m.asset_id in ok_assets]
        live = [m for m in targets if m.moment_id not in excluded]
        if not targets:
            notes.append(f"must_include {ref!r} matched no available video or image moment")
            continue
        if not live:
            notes.append(f"must_include {ref!r} conflicts with must_exclude; the exclusion won")
            continue
        exact = [m for m in live if ref.strip() in (m.moment_id, m.segment_id)]
        if exact:
            ids |= {m.moment_id for m in exact}
            continue
        by_asset: dict[str, list[MomentInfo]] = {}
        for m in live:
            by_asset.setdefault(m.asset_id, []).append(m)
        for aid, ms in by_asset.items():
            a = catalog.assets[aid]
            best = sorted(ms, key=lambda m: (-interest_score(m, a), m.source_in_seconds or 0.0, m.moment_id))
            ids |= {m.moment_id for m in best[: s.max_similar_shots_per_scene]}
    return ids


def _assign_durations(cands: list[_Cand], s: BaselineSettings) -> None:
    first, last = cands[0], cands[-1]
    for c in cands:
        if c.asset.media_type == "image":
            continue
        cap = s.clip_max_seconds
        if c is first:
            cap = max(cap, s.opening_hook_seconds / 2)
        if c is last:
            cap = max(cap, s.closing_seconds / 2)
        length = c.duration
        c.duration = round(length if length < s.clip_min_seconds else min(length, cap), 3)


def _cap_similar(cands: list[_Cand], s: BaselineSettings) -> list[_Cand]:
    groups: list[list[_Cand]] = []
    for c in cands:
        if groups and _similar(groups[-1][-1], c, s):
            groups[-1].append(c)
        else:
            groups.append([c])
    keep: set[int] = set()
    for g in groups:
        forced = [c for c in g if c.must]
        free = sorted((c for c in g if not c.must), key=lambda c: (-c.interest, c.order))
        keep |= {c.order for c in forced} | {c.order for c in free[: max(0, s.max_similar_shots_per_scene - len(forced))]}
    return [c for c in cands if c.order in keep]


def _choose(cands: list[_Cand], s: BaselineSettings, notes: list[str]) -> list[_Cand]:
    by_scene: dict[int, list[_Cand]] = {}
    for c in cands:
        by_scene.setdefault(c.scene, []).append(c)
    for lst in by_scene.values():
        for r, c in enumerate(sorted(lst, key=lambda c: (-c.interest, c.order))):
            c.rank = r
    opening, closing = cands[0], cands[-1]
    for c in cands:
        c.tier = 0 if c.must else 1 if (c is opening or c is closing) else 2
    chosen: list[_Cand] = []
    total = 0.0
    for c in sorted(cands, key=lambda c: (c.tier, c.rank if c.tier == 2 else 0, c.scene, c.order)):
        if c.tier < 2 or total + c.duration <= s.target_duration_seconds + BUDGET_SLACK_SECONDS:
            chosen.append(c)
            total += c.duration
    if total < s.target_duration_seconds - max(2.0, 0.05 * s.target_duration_seconds):
        notes.append(f"usable footage ({total:.1f}s) is shorter than the {s.target_duration_seconds:g}s target")
    elif total > s.target_duration_seconds + max(2.0, 0.05 * s.target_duration_seconds):
        notes.append(f"must_include/opening/closing clips push the plan to {total:.1f}s, over the {s.target_duration_seconds:g}s target")
    return sorted(chosen, key=lambda c: c.order)


def _scene_title(group: list[_Cand], n: int) -> tuple[str, str]:
    a = group[0].asset
    when = a.capture_time[:10] if a.capture_time else ""
    folder = a.folder.rpartition("/")[2]
    title = folder or when or f"Scene {n}"
    where = f"folder {a.folder!r}" if a.folder else "media root"
    purpose = f"Chronological group ({where}{', ' + when if when else ''})"
    return title, purpose


def _to_selection(chosen: list[_Cand], s: BaselineSettings, notes: list[str]) -> SelectionPlan:
    groups: list[list[_Cand]] = []
    for c in chosen:
        if groups and groups[-1][0].scene == c.scene:
            groups[-1].append(c)
        else:
            groups.append([c])
    scenes = []
    n_clip = 0
    for gi, g in enumerate(groups, start=1):
        clips = []
        for c in g:
            n_clip += 1
            f = c.moment.features
            sound = (c.asset.media_type == "video" and c.asset.has_audio and f.get("silent") is not True and s.preserve_natural_audio)
            kind = "photo" if c.asset.media_type == "image" else f"segment {c.index_in_asset + 1} of {c.count_in_asset}"
            why = "required by must_include" if c.must else "chronological pick"
            clips.append(SelectionClip(
                clip_instance_id=f"clip_{n_clip:03d}", moment_id=c.moment.moment_id, target_duration_seconds=c.duration,
                anchor="center", track_role="primary_video", audio_policy="natural_sound" if sound else "muted",
                transition_in=SelectionTransition(type="cut", duration_seconds=0.0),
                rationale=f"Baseline ({why}): {c.asset.relative_path}, {kind}, interest {c.interest:.2f}"))
        title, purpose = _scene_title(g, gi)
        scenes.append(SelectionScene(scene_id=f"scene_{gi:03d}", title=title, purpose=purpose,
                                     target_duration_seconds=round(sum(c.target_duration_seconds for c in clips), 3), clips=clips))
    return SelectionPlan(schema_version=SCHEMA_VERSION, planner="baseline", scenes=scenes, uncertainties=notes)
