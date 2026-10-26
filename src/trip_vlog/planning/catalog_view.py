"""Read-only planning view of the catalog: plain dataclasses so planning code is testable without SQLite or ffmpeg."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from trip_vlog.paths import nfc


@dataclass(frozen=True)
class AssetInfo:
    asset_id: str
    relative_path: str
    media_type: str  # video | image | audio
    duration_seconds: float | None = None
    fps: Fraction | None = None
    width: int | None = None
    height: int | None = None
    display_width: int | None = None   # after rotation
    display_height: int | None = None
    audio_streams: int = 0
    content_version: str | None = None
    capture_time: str | None = None
    present: bool = True
    probe_ok: bool = True
    analysis_status: str = "complete"
    rotation_degrees: int = 0          # clockwise; for stills FilmCraft ignores it (it shows the stored pixels)

    @property
    def has_audio(self) -> bool:
        return self.media_type == "audio" or self.audio_streams > 0

    @property
    def folder(self) -> str:
        return self.relative_path.rpartition("/")[0]

    @property
    def orientation(self) -> str | None:
        w, h = self.display_width or self.width, self.display_height or self.height
        if not w or not h:
            return None
        return "portrait" if h > w else "landscape" if w > h else "square"


@dataclass(frozen=True)
class MomentInfo:
    moment_id: str
    asset_id: str
    segment_id: str | None = None
    source_in_seconds: float | None = None   # None for still images
    source_out_seconds: float | None = None
    best_in_seconds: float | None = None     # optional sub-range preferred by anchor="best" (set by later phases)
    best_out_seconds: float | None = None
    content_version: str | None = None
    tags: tuple[str, ...] = ()
    features: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None

    @property
    def length_seconds(self) -> float | None:
        if self.source_in_seconds is None or self.source_out_seconds is None:
            return None
        return self.source_out_seconds - self.source_in_seconds


class PlanningCatalog:
    """Assets and moments indexed by ID. Build with `from_catalog` (SQLite) or directly in tests."""

    def __init__(self, assets: Iterable[AssetInfo], moments: Iterable[MomentInfo]):
        self.assets: dict[str, AssetInfo] = {a.asset_id: a for a in assets}
        self.moments: dict[str, MomentInfo] = {m.moment_id: m for m in moments}

    @classmethod
    def from_catalog(cls, catalog: Any) -> "PlanningCatalog":
        assets = []
        for r in catalog.list_assets(present_only=False):
            num, den = r["frame_rate_num"], r["frame_rate_den"]
            assets.append(AssetInfo(
                asset_id=r["asset_id"], relative_path=r["relative_path"], media_type=r["media_type"] or "unknown",
                duration_seconds=r["duration_seconds"], fps=Fraction(num, den) if num and den else None,
                width=r["width"], height=r["height"], display_width=r["display_width"], display_height=r["display_height"],
                rotation_degrees=int(r["rotation_degrees"] or 0),
                audio_streams=int(r["audio_streams"] or 0), content_version=r["content_version"],
                capture_time=r["capture_time"], present=bool(r["present"]), probe_ok=r["probe_status"] == "ok",
                analysis_status=r["analysis_status"]))
        moments = []
        for r in catalog.list_moments():
            d = json.loads(r["data_json"] or "{}")
            moments.append(MomentInfo(
                moment_id=r["moment_id"], asset_id=r["asset_id"], segment_id=r["segment_id"],
                source_in_seconds=r["source_in_seconds"], source_out_seconds=r["source_out_seconds"],
                best_in_seconds=d.get("best_in_seconds"), best_out_seconds=d.get("best_out_seconds"),
                content_version=r["content_version"], tags=tuple(d.get("editorial_tags", ())),
                features=d.get("features", {}), description=d.get("description")))
        return cls(assets, moments)

    def moments_for_asset(self, asset_id: str) -> list[MomentInfo]:
        return sorted((m for m in self.moments.values() if m.asset_id == asset_id),
                      key=lambda m: (m.source_in_seconds if m.source_in_seconds is not None else -1.0, m.moment_id))

    def fingerprint(self, moment_ids: Iterable[str] | None = None) -> list[Any]:
        """JSON-able description of everything the resolver and the validators read from the catalog (for stage input hashes):
        a changed range, best sub-range, length, frame rate, audio or version re-plans, even when the text a model sees is the same.
        With ``moment_ids``: only those moments and their assets."""
        moments = self.moments.values() if moment_ids is None else [self.moments[i] for i in set(moment_ids) if i in self.moments]
        asset_ids = {m.asset_id for m in moments} if moment_ids is not None else None
        assets = [a for a in self.assets.values() if asset_ids is None or a.asset_id in asset_ids]
        return [sorted((a.asset_id, a.relative_path, a.media_type, a.duration_seconds, str(a.fps) if a.fps is not None else None,
                        a.display_width or a.width, a.display_height or a.height, a.audio_streams, a.content_version, a.present, a.probe_ok,
                        a.capture_time) for a in assets),
                sorted((m.moment_id, m.asset_id, m.source_in_seconds, m.source_out_seconds, m.best_in_seconds, m.best_out_seconds, m.content_version)
                       for m in moments)]


# ---- references (must_include / must_exclude) ----------------------------------------------------------

def required_label(n: int) -> str:
    """How a request or a finding sent to a model names the n-th must_include reference (1-based; 0 = unknown): never the reference."""
    return f"required item {n}" if n > 0 else "a required item"


def _norm_ref(ref: str) -> str:
    r = nfc(ref.strip()).replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    return r


def ref_matches(ref: str, asset: AssetInfo, moment: MomentInfo | None = None) -> bool:
    """A reference names a moment_id, segment_id, asset_id, a relative path, a glob ('day-02/*.mp4') or a folder ('day-02/')."""
    r = _norm_ref(ref)
    if not r:
        return False
    if moment is not None and r in (moment.moment_id, moment.segment_id):
        return True
    if r == asset.asset_id:
        return True
    path = asset.relative_path
    if r.endswith("/"):
        return path.startswith(r)
    if any(c in r for c in "*?["):
        return fnmatch.fnmatchcase(path, r)
    return path == r


def ref_targets(ref: str, catalog: PlanningCatalog) -> list[MomentInfo]:
    """All moments a reference resolves to (empty if it matches nothing in the catalog)."""
    out = []
    for m in catalog.moments.values():
        a = catalog.assets.get(m.asset_id)
        if a is not None and ref_matches(ref, a, m):
            out.append(m)
    return sorted(out, key=lambda m: (m.asset_id, m.source_in_seconds if m.source_in_seconds is not None else -1.0, m.moment_id))


def capture_sort_key(capture_time: str | None) -> tuple[int, float]:
    """(0, epoch) for known capture times, (1, 0) for unknown. Aware times use UTC; naive wall-clock times are compared
    as if UTC (ordering only; the stored value is never converted)."""
    if not capture_time:
        return (1, 0.0)
    try:
        dt = datetime.fromisoformat(capture_time)
    except ValueError:
        return (1, 0.0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (0, dt.timestamp())
