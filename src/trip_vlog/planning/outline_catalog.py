"""Compact catalog view for the story editor (DESIGN §8.1, §9.5): one small record per candidate moment.

Every record has a *trusted header* (a compact JSON object made only of IDs, numbers and enum values produced by code) and an
optional *untrusted text block* with everything derived from the media itself (the vision description, a verbatim transcript
excerpt and the file's base name). Free text is never placed in the header and is always wrapped by `llm.untrusted`.

Order is deterministic: capture time (unknown last), relative path, offset in the clip, moment id. When more moments are
eligible than `planning.max_moments_in_prompt`, `preselect` keeps all must_include moments, then the best moment of every
duplicate group and time cluster, then the rest by interest; it records what it dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Sequence

from trip_vlog.analysis.labels import EDITORIAL_TAGS
from trip_vlog.llm.untrusted import untrusted_block
from trip_vlog.planning.catalog_view import PlanningCatalog, capture_sort_key, ref_matches, ref_targets, required_label
from trip_vlog.state import stable_hash

QUALITY_KEYS = ("sharpness", "stability", "exposure")
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_DESCRIPTION_CHARS = 400
MAX_DROPPED_LISTED = 200


@dataclass(frozen=True)
class OutlineMoment:
    moment_id: str
    asset_id: str
    kind: str                         # video | photo
    sort_key: tuple[Any, ...]
    capture: str | None               # local wall-clock "YYYY-MM-DD HH:MM", never converted, never GPS
    date: str | None = None           # "YYYY-MM-DD"
    offset_seconds: float | None = None   # position of a video moment inside its clip
    duration_seconds: float = 0.0
    description: str | None = None
    tags: tuple[str, ...] = ()
    quality: tuple[tuple[str, str | None], ...] = ()
    interest: float | None = None
    confidence: float | None = None
    uncertain: bool = False
    labeled: bool = False
    audio_tags: tuple[str, ...] = ()
    duplicate_group: str | None = None
    orientation: str | None = None
    transcript: str = ""
    filename: str | None = None
    day: int | None = None            # 1-based day of the trip (distinct capture dates, whole eligible catalog)
    index: int = 0                    # 1-based chronological position within the prompt

    @property
    def rank_interest(self) -> float:
        return self.interest if self.interest is not None else 0.0

    def header(self) -> dict[str, Any]:
        """Trusted fields only. Null fields are omitted to keep the prompt small."""
        h: dict[str, Any] = {"id": self.moment_id, "n": self.index, "kind": self.kind}
        for k, v in (("when", self.capture), ("day", self.day), ("at", self.offset_seconds), ("dur", round(self.duration_seconds, 1)),
                     ("orient", self.orientation), ("interest", self.interest), ("conf", self.confidence),
                     ("dup", self.duplicate_group)):
            if v is not None:
                h[k] = v
        if self.uncertain:
            h["uncertain"] = True
        if not self.labeled:
            h["unlabeled"] = True
        q = {k: v for k, v in self.quality if v}
        if q:
            h["quality"] = q
        if self.tags:
            h["tags"] = list(self.tags)
        if self.audio_tags:
            h["audio"] = list(self.audio_tags)
        if self.transcript:
            h["speech"] = True
        return h

    def text_block(self) -> str | None:
        lines = []
        if self.description:
            lines.append(f"description: {self.description}")
        if self.transcript:
            lines.append(f"speech: {self.transcript}")
        if self.filename:
            lines.append(f"file: {self.filename}")
        return untrusted_block("moment", "\n".join(lines), max_chars=10_000) if lines else None

    def render(self) -> str:
        head = json.dumps(self.header(), ensure_ascii=False, separators=(",", ":"))
        block = self.text_block()
        return head if block is None else head + "\n" + block


@dataclass
class SelectionReport:
    """What went into the prompt and what was left out, and why."""
    limit: int
    eligible: int                               # moments of present, usable video/photo assets
    excluded_by_must_exclude: int = 0
    kept: int = 0
    over_limit: bool = False
    must_include_kept: int = 0
    must_include_overflow: bool = False
    dropped: list[dict[str, str]] = field(default_factory=list)   # {"moment_id", "reason"}; capped at MAX_DROPPED_LISTED
    dropped_total: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)
    unmatched_must_include: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "eligible": self.eligible, "excluded_by_must_exclude": self.excluded_by_must_exclude,
                "kept": self.kept, "over_limit": self.over_limit, "must_include_kept": self.must_include_kept,
                "must_include_overflow": self.must_include_overflow, "dropped_total": self.dropped_total,
                "dropped_by_reason": dict(sorted(self.dropped_by_reason.items())), "dropped": self.dropped,
                "unmatched_must_include": self.unmatched_must_include}


@dataclass
class OutlineCatalog:
    moments: list[OutlineMoment]                # in prompt order (what the model is shown)
    eligible: dict[str, OutlineMoment]          # every usable, non-excluded moment (a user-edited outline may use these)
    all_ids: frozenset[str]                     # every moment in the catalog, usable or not (for precise findings)
    excluded_ids: frozenset[str]                # matched by must_exclude
    must_include: dict[str, list[str]]          # ref -> available (eligible) moment ids it resolves to
    selection: SelectionReport

    @property
    def offered(self) -> frozenset[str]:
        return frozenset(m.moment_id for m in self.moments)

    def ref_label(self, ref: str) -> str:
        """What a request says instead of a must_include reference ("required item 2"): references are paths, folders or globs
        taken from the user's own layout and stay on this machine. Numbered in the order of ``editorial.must_include``."""
        return required_label(list(self.must_include).index(ref) + 1 if ref in self.must_include else 0)

    def by_id(self, moment_id: str) -> OutlineMoment | None:
        return self.eligible.get(moment_id)

    def render(self) -> str:
        return "\n".join(m.render() for m in self.moments)

    def digest(self) -> str:
        """Identity of exactly what the model sees (changes when a label, transcript, capture time or selection changes)."""
        return stable_hash([m.render() for m in self.moments])


# ---- building ------------------------------------------------------------------------------------------

def _clean(text: Any, limit: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _capture(ct: str | None) -> tuple[str | None, str | None]:
    if not ct:
        return None, None
    try:
        dt = datetime.fromisoformat(ct)
    except ValueError:
        return None, None
    return dt.strftime("%Y-%m-%d %H:%M"), dt.strftime("%Y-%m-%d")


def _tokens(values: Iterable[Any], allowed: Sequence[str] | None = None) -> tuple[str, ...]:
    out = []
    for v in values or ():
        v = str(v)
        if _SAFE_TOKEN.match(v) and (allowed is None or v in allowed) and v not in out:
            out.append(v)
    return tuple(out)


def _num(v: Any) -> float | None:
    return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_moments(catalog: Any, *, still_seconds: float, excerpt_chars: int, send_filenames: bool) -> list[OutlineMoment]:
    """Every moment of a present, readable video/photo asset, in chronological order (index/day not yet assigned)."""
    assets = {r["asset_id"]: r for r in catalog.list_assets(present_only=True)
              if r["probe_status"] == "ok" and r["media_type"] in ("video", "image")}
    transcripts: dict[str, dict[str, str]] = {}
    if excerpt_chars:
        for r in catalog.list_transcripts():
            d = json.loads(r["data_json"] or "{}")
            if r["asset_id"] in assets and r["content_version"] == assets[r["asset_id"]]["content_version"]:
                transcripts.setdefault(r["asset_id"], {})[d.get("transcript_id") or r["transcript_id"]] = d.get("text") or r["text"] or ""
    out = []
    for r in catalog.list_moments():
        a = assets.get(r["asset_id"])
        if a is None or r["content_version"] != a["content_version"]:
            continue  # moment of a missing asset or of a superseded content version
        d = json.loads(r["data_json"] or "{}")
        video = a["media_type"] == "video"
        s_in, s_out = r["source_in_seconds"], r["source_out_seconds"]
        dur = (s_out - s_in) if video and s_in is not None and s_out is not None else still_seconds
        if dur <= 0:
            continue
        labeled = bool(d.get("labeled"))
        when, date = _capture(a["capture_time"])
        w, h = a["display_width"] or a["width"], a["display_height"] or a["height"]
        orient = ("portrait" if h > w else "landscape" if w > h else "square") if w and h else None
        text = ""
        if excerpt_chars and d.get("transcript_refs"):
            tx = transcripts.get(r["asset_id"], {})
            text = _clean(" ".join(tx[t] for t in d["transcript_refs"] if t in tx), excerpt_chars)
        q = d.get("quality") or {}
        out.append(OutlineMoment(
            moment_id=r["moment_id"], asset_id=r["asset_id"], kind="video" if video else "photo",
            sort_key=(*capture_sort_key(a["capture_time"]), a["relative_path"], s_in if s_in is not None else -1.0, r["moment_id"]),
            capture=when, date=date, offset_seconds=round(s_in, 1) if video and s_in is not None else None,
            duration_seconds=float(dur), description=_clean(d["description"], MAX_DESCRIPTION_CHARS) if d.get("description") else None,
            tags=_tokens(d.get("editorial_tags"), EDITORIAL_TAGS) if labeled else (),
            quality=tuple((k, q.get(k) if q.get(k) in ("low", "ok", "good") else None) for k in QUALITY_KEYS),
            interest=_num(d.get("interest")), confidence=_num(d.get("confidence")), uncertain=bool(d.get("uncertain")),
            labeled=labeled, audio_tags=_tokens(d.get("audio_tags")),
            duplicate_group=d.get("duplicate_group") if isinstance(d.get("duplicate_group"), str) and _SAFE_TOKEN.match(d["duplicate_group"]) else None,
            orientation=orient, transcript=text,
            filename=_clean(a["relative_path"].rpartition("/")[2], 120) if send_filenames else None))
    out.sort(key=lambda m: m.sort_key)
    return out


def _clusters(moments: Sequence[OutlineMoment], gap_seconds: float, catalog_times: dict[str, float | None]) -> dict[str, int]:
    """moment_id -> cluster number: chronologically consecutive moments closer than `gap_seconds`. Moments without a capture
    time cluster by asset (they have no usable clock)."""
    out: dict[str, int] = {}
    cluster = -1
    prev_t: float | None = None
    prev_asset: str | None = None
    for m in moments:
        t = catalog_times.get(m.asset_id)
        if t is None:
            new = m.asset_id != prev_asset or prev_t is not None
        else:
            new = prev_t is None or (t + (m.offset_seconds or 0.0)) - prev_t > gap_seconds
        if new:
            cluster += 1
        out[m.moment_id] = cluster
        prev_t = None if t is None else t + (m.offset_seconds or 0.0) + m.duration_seconds
        prev_asset = m.asset_id
    return out


def preselect(moments: Sequence[OutlineMoment], limit: int, mandatory: set[str], *, cluster_gap_seconds: float,
              epoch_of_asset: dict[str, float | None]) -> tuple[list[OutlineMoment], list[tuple[str, str]]]:
    """Keep at most `limit` moments (mandatory ones always stay). Returns (kept in chronological order, [(moment_id, reason)]).

    Priority after the mandatory set: (1) the best-interest moment of each time cluster, among duplicate-group
    representatives; (2) the other representatives of duplicate groups (best of each group); (3) lower-interest members of
    duplicate groups. Within a tier: higher interest first, then chronological order. Fully deterministic.
    """
    if len(moments) <= limit:
        return list(moments), []
    order = {m.moment_id: i for i, m in enumerate(moments)}

    def best(ms: Iterable[OutlineMoment]) -> OutlineMoment:
        return min(ms, key=lambda m: (-m.rank_interest, order[m.moment_id]))

    groups: dict[str, list[OutlineMoment]] = {}
    for m in moments:
        if m.duplicate_group:
            groups.setdefault(m.duplicate_group, []).append(m)
    group_best = {g: best(ms).moment_id for g, ms in groups.items()}
    reps = [m for m in moments if not m.duplicate_group or group_best[m.duplicate_group] == m.moment_id]
    clusters = _clusters(moments, cluster_gap_seconds, epoch_of_asset)
    by_cluster: dict[int, list[OutlineMoment]] = {}
    for m in reps:
        by_cluster.setdefault(clusters[m.moment_id], []).append(m)
    cluster_best = {best(ms).moment_id for ms in by_cluster.values()}
    rep_ids = {m.moment_id for m in reps}
    tiers: list[list[OutlineMoment]] = [
        [m for m in reps if m.moment_id in cluster_best],
        [m for m in reps if m.moment_id not in cluster_best],
        [m for m in moments if m.moment_id not in rep_ids]]
    keep = {m.moment_id for m in moments if m.moment_id in mandatory}
    reasons: dict[str, str] = {}
    for ti, tier in enumerate(tiers):
        for m in sorted(tier, key=lambda m: (-m.rank_interest, order[m.moment_id])):
            if m.moment_id in keep:
                continue
            if len(keep) < limit:
                keep.add(m.moment_id)
            else:
                reasons[m.moment_id] = "duplicate_group_member" if ti == 2 else "below_interest_cutoff"
    kept = [m for m in moments if m.moment_id in keep]
    dropped = [(m.moment_id, reasons[m.moment_id]) for m in moments if m.moment_id not in keep]
    return kept, dropped


def build_outline_catalog(catalog: Any, config: Any) -> OutlineCatalog:
    """Compact, deterministic catalog for the story editor, honoring must_exclude / must_include and the prompt size limit."""
    pl, ed, media = config.planning, config.editorial, config.media
    view = PlanningCatalog.from_catalog(catalog)
    moments = build_moments(catalog, still_seconds=float(media.still_duration_seconds), excerpt_chars=pl.transcript_excerpt_chars,
                            send_filenames=media.vision_send_filenames)
    excluded: set[str] = set()
    for ref in ed.must_exclude:
        for mid, mi in view.moments.items():
            a = view.assets.get(mi.asset_id)
            if a is not None and ref_matches(ref, a, mi):
                excluded.add(mid)
    eligible_all = [m for m in moments if m.moment_id not in excluded]
    # day numbers come from the whole eligible catalog so they do not shift when pre-selection drops a day's moments
    dates = sorted({m.date for m in eligible_all if m.date})
    day_of = {d: i + 1 for i, d in enumerate(dates)}
    eligible_all = [replace(m, day=day_of.get(m.date) if m.date else None) for m in eligible_all]
    usable = {m.moment_id for m in eligible_all}
    must: dict[str, list[str]] = {}
    for ref in ed.must_include:
        must[ref] = [m.moment_id for m in ref_targets(ref, view) if m.moment_id in usable]
    mandatory = {mid for ids in must.values() for mid in ids}
    epoch: dict[str, float | None] = {}
    for m in eligible_all:
        a = view.assets.get(m.asset_id)
        k = capture_sort_key(a.capture_time if a else None)
        epoch[m.asset_id] = k[1] if k[0] == 0 else None
    rep = SelectionReport(limit=pl.max_moments_in_prompt, eligible=len(eligible_all), excluded_by_must_exclude=len(excluded & {m.moment_id for m in moments}),
                          over_limit=len(eligible_all) > pl.max_moments_in_prompt, unmatched_must_include=[r for r, ids in must.items() if not ids])
    kept, dropped = preselect(eligible_all, pl.max_moments_in_prompt, mandatory, cluster_gap_seconds=pl.cluster_gap_seconds, epoch_of_asset=epoch)
    kept = [replace(m, index=i + 1) for i, m in enumerate(kept)]
    rep.kept = len(kept)
    rep.must_include_kept = len(mandatory & {m.moment_id for m in kept})
    rep.must_include_overflow = len(mandatory) > pl.max_moments_in_prompt
    rep.dropped_total = len(dropped)
    for _, why in dropped:
        rep.dropped_by_reason[why] = rep.dropped_by_reason.get(why, 0) + 1
    rep.dropped = [{"moment_id": mid, "reason": why} for mid, why in dropped[:MAX_DROPPED_LISTED]]
    return OutlineCatalog(kept, {m.moment_id: m for m in eligible_all}, frozenset(view.moments), frozenset(excluded), must, rep)
