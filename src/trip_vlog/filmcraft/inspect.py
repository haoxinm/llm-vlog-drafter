"""Normalised read-back of FilmCraft state and verification against a TimelineSpec (DESIGN §12.1, §13.2).

``sequence_inspect`` / ``project_inspect`` / ``media.status`` payloads are parsed into small immutable
snapshots (frames AND exact ticks, track names normalised to ``V1`` / ``A2``). ``verify_against_spec``
compares a sequence snapshot with the spec it was built from: clip order, track, start and duration within
one frame, source in/out, gain, transitions, markers, titles, media online and expected path, and flags
every clip the spec does not account for (that is how duplicated clips are detected).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.mappings import ClipMap, ClipRef, MediaMap, canon_path
from trip_vlog.filmcraft.timebase import TICKS_PER_SECOND, Timebase
from trip_vlog.filmcraft.timeline_spec import TimelineSpec, fit_scale_percent


class SnapshotParseError(ValueError):
    pass


@dataclass(frozen=True)
class ClipSnapshot:
    clip_id: int
    item: int | None
    name: str
    track: str  # "V1" / "A2"
    kind: str  # "video" | "audio"
    start_frame: int
    end_frame: int
    duration_frames: int
    start_ticks: int
    duration_ticks: int
    source_in_ticks: int
    source_out_ticks: int
    gain_db: float
    speed: float
    link: int | None
    enabled: bool
    scale: float | None = None  # video: Motion scale in percent as read back (None when the payload has no motion effect)
    volume_keyframes: int = 0   # audio: number of keyframes on the Volume level (values are not part of the read-back)

    @property
    def source_in_seconds(self) -> Fraction:
        return Fraction(self.source_in_ticks, TICKS_PER_SECOND)

    @property
    def source_out_seconds(self) -> Fraction:
        return Fraction(self.source_out_ticks, TICKS_PER_SECOND)


@dataclass(frozen=True)
class TransitionSnapshot:
    transition_id: int | None
    track: str
    effect: str
    duration_ticks: int
    start_ticks: int
    align: str | None
    from_clip: int | None
    to_clip: int | None


@dataclass(frozen=True)
class MarkerSnapshot:
    marker_id: int | None
    name: str
    comment: str
    color: str | None
    start_ticks: int


@dataclass(frozen=True)
class TrackSnapshot:
    name: str  # "V1"
    engine_name: str  # "Video 1"
    kind: str
    track_id: int
    clips: tuple[ClipSnapshot, ...]
    transitions: tuple[TransitionSnapshot, ...]


@dataclass(frozen=True)
class SequenceSnapshot:
    sequence_id: int | None
    name: str
    width: int
    height: int
    fps: Fraction
    duration_frames: int
    tracks: tuple[TrackSnapshot, ...]
    markers: tuple[MarkerSnapshot, ...]

    def track(self, name: str) -> TrackSnapshot | None:
        return next((t for t in self.tracks if t.name == name), None)

    def clips(self) -> list[ClipSnapshot]:
        return [c for t in self.tracks for c in t.clips]

    @property
    def clip_count(self) -> int:
        return sum(len(t.clips) for t in self.tracks)

    @property
    def transitions(self) -> list[TransitionSnapshot]:
        return [tr for t in self.tracks for tr in t.transitions]

    def find_clip(self, clip_id: int) -> ClipSnapshot | None:
        return next((c for c in self.clips() if c.clip_id == clip_id), None)

    @property
    def timebase(self) -> Timebase:
        return Timebase(self.fps)


@dataclass(frozen=True)
class MediaStatus:
    item: int
    name: str
    path: str | None
    status: str  # "online" | "offline" | ...
    fingerprint: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class ProjectItem:
    item: int
    name: str
    type: str  # Movie | Still Image | Audio | Sequence | Graphic | ...
    duration_ticks: int | None

    @property
    def duration_seconds(self) -> Fraction | None:
        return None if self.duration_ticks is None else Fraction(self.duration_ticks, TICKS_PER_SECOND)


@dataclass(frozen=True)
class ProjectSnapshot:
    name: str
    path: str | None
    dirty: bool
    active_sequence: int | None
    revision: int | None
    items: tuple[ProjectItem, ...]
    media: tuple[MediaStatus, ...] = ()

    def media_by_item(self) -> dict[int, MediaStatus]:
        return {m.item: m for m in self.media}

    def item(self, item_id: int) -> ProjectItem | None:
        return next((i for i in self.items if i.item == item_id), None)

    def sequences(self) -> list[ProjectItem]:
        return [i for i in self.items if i.type == "Sequence"]


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------


def _need(d: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in d:
        raise SnapshotParseError(f"{what}: FilmCraft payload has no '{key}' field (schema changed?)")
    return d[key]


_FLOAT_VALUE = re.compile(r"^Float\((-?[0-9.eE+-]+)\)$")


def _effect_param(clip: Mapping[str, Any], effect: str, param: str) -> Mapping[str, Any] | None:
    for e in clip.get("effects") or []:
        if isinstance(e, Mapping) and e.get("effect") == effect:
            p = (e.get("params") or {}).get(param)
            return p if isinstance(p, Mapping) else None
    return None


def _motion_scale(clip: Mapping[str, Any]) -> float | None:
    p = _effect_param(clip, "motion", "scale")
    m = _FLOAT_VALUE.match(str(p.get("value"))) if p else None
    return float(m[1]) if m else None


def _volume_keyframes(clip: Mapping[str, Any]) -> int:
    p = _effect_param(clip, "volume", "level")
    try:
        return int(p.get("keyframes", 0)) if p else 0
    except (TypeError, ValueError):
        return 0


def parse_sequence(payload: Any) -> SequenceSnapshot:
    if not isinstance(payload, Mapping):
        raise SnapshotParseError(f"sequence_inspect returned {type(payload).__name__}, not an object: {str(payload)[:200]}")
    settings = _need(payload, "settings", "sequence_inspect")
    fr = _need(settings, "frame_rate", "sequence settings")
    fps = Fraction(int(fr["num"]), int(fr["den"]))
    tracks: list[TrackSnapshot] = []
    for kind, prefix in (("video", "V"), ("audio", "A")):
        for idx, t in enumerate(payload.get(kind) or [], start=1):
            name = f"{prefix}{idx}"
            clips = []
            for c in t.get("items", []):
                clips.append(ClipSnapshot(
                    clip_id=int(_need(c, "clip", "clip")), item=c.get("item"), name=str(c.get("name", "")), track=name,
                    kind=kind, start_frame=int(_need(c, "startFrame", "clip")), end_frame=int(_need(c, "endFrame", "clip")),
                    duration_frames=int(c.get("durationFrames", int(c["endFrame"]) - int(c["startFrame"]))),
                    start_ticks=int(_need(c, "start", "clip")), duration_ticks=int(c.get("duration", 0)),
                    source_in_ticks=int(c.get("sourceIn", 0)), source_out_ticks=int(c.get("sourceOut", 0)),
                    gain_db=float(c.get("gainDb") or 0.0), speed=float(c.get("speed", 1.0)), link=c.get("link"),
                    enabled=bool(c.get("enabled", True)),
                    scale=_motion_scale(c) if kind == "video" else None,
                    volume_keyframes=_volume_keyframes(c) if kind == "audio" else 0))
            clips.sort(key=lambda c: (c.start_ticks, c.clip_id))
            trs = tuple(TransitionSnapshot(
                transition_id=tr.get("id"), track=name, effect=str(tr.get("effect", "")),
                duration_ticks=int(tr.get("duration", 0)), start_ticks=int(tr.get("start", 0)), align=tr.get("align"),
                from_clip=tr.get("from"), to_clip=tr.get("to")) for tr in t.get("transitions", []))
            tracks.append(TrackSnapshot(name, str(t.get("name", "")), kind, int(t.get("id", -1)), tuple(clips), trs))
    markers = tuple(MarkerSnapshot(m.get("id"), str(m.get("name", "")), str(m.get("comment") or ""), m.get("color"),
                                   int(m.get("start", 0))) for m in payload.get("markers") or [])
    return SequenceSnapshot(
        sequence_id=payload.get("id"), name=str(payload.get("name", "")), width=int(settings["width"]),
        height=int(settings["height"]), fps=fps, duration_frames=int(payload.get("durationFrames", 0)),
        tracks=tuple(tracks), markers=markers)


def parse_media_status(payload: Any) -> tuple[MediaStatus, ...]:
    if payload in (None, ""):
        return ()
    if not isinstance(payload, list):
        raise SnapshotParseError(f"media.status returned {type(payload).__name__}, not a list: {str(payload)[:200]}")
    out = []
    for r in payload:
        ident = r.get("identity") or {}
        out.append(MediaStatus(item=int(r["item"]), name=str(r.get("name", "")), path=r.get("path"),
                               status=str(r.get("status", "")), fingerprint=ident.get("fingerprint"), size=ident.get("size")))
    return tuple(out)


def parse_project(payload: Any, media: Sequence[MediaStatus] = ()) -> ProjectSnapshot:
    if not isinstance(payload, Mapping):
        raise SnapshotParseError(f"project_inspect returned {type(payload).__name__}, not an object: {str(payload)[:200]}")
    root = _need(payload, "root", "project_inspect")
    items = tuple(ProjectItem(item=int(c["item"]), name=str(c.get("name", "")), type=str(c.get("type", "")),
                              duration_ticks=c.get("duration")) for c in root.get("children", []) if "item" in c)
    return ProjectSnapshot(name=str(payload.get("name", root.get("name", ""))), path=payload.get("path"),
                           dirty=bool(payload.get("dirty", False)), active_sequence=payload.get("activeSequence"),
                           revision=payload.get("revision"), items=items, media=tuple(media))


# ----------------------------------------------------------------------------
# verification
# ----------------------------------------------------------------------------

FIT_SCALE_TOLERANCE = 0.5     # percent points


def _check_framing(add: Any, spec: TimelineSpec, p: Any, c: ClipSnapshot) -> int:
    """A placement with ``fit_source_size`` must read back with the Motion scale that fits the source in the frame."""
    if p.fit_source_size is None:
        return 0
    want = fit_scale_percent(spec.width, spec.height, *p.fit_source_size)
    if c.scale is None or abs(c.scale - want) > FIT_SCALE_TOLERANCE:
        add("framing", p.clip_instance_id, round(want, 2), c.scale,
            f"{p.clip_instance_id}: scale {c.scale} read back, {round(want, 2)} expected to fit {p.fit_source_size[0]}x{p.fit_source_size[1]} "
            f"in {spec.width}x{spec.height}")
    return 1


@dataclass(frozen=True)
class Mismatch:
    kind: str
    subject: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "expected": self.expected, "actual": self.actual,
                "message": self.message}

    def __str__(self) -> str:
        return f"[{self.kind}] {self.subject}: {self.message}"


@dataclass
class VerificationReport:
    mismatches: list[Mismatch] = field(default_factory=list)
    clip_map: ClipMap = field(default_factory=ClipMap)
    checked: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "mismatches": [m.to_dict() for m in self.mismatches], "checked": self.checked,
                "skipped": self.skipped}

    def summary(self, limit: int = 10) -> str:
        if self.ok:
            return "timeline matches the spec"
        head = "; ".join(str(m) for m in self.mismatches[:limit])
        more = f" (+{len(self.mismatches) - limit} more)" if len(self.mismatches) > limit else ""
        return f"{len(self.mismatches)} mismatch(es): {head}{more}"


def verify_against_spec(
    snapshot: SequenceSnapshot,
    spec: TimelineSpec,
    *,
    project: ProjectSnapshot | None = None,
    media_map: MediaMap | None = None,
    tolerance_frames: int = 1,
    gain_tolerance_db: float = 0.05,
) -> VerificationReport:
    """Compare a read-back sequence with ``spec``. See module docstring for what is checked."""
    rep = VerificationReport()
    mm = rep.mismatches
    tol = tolerance_frames
    fps = snapshot.fps
    tol_s = Fraction(tol) / fps + Fraction(1, 10**6)

    def add(kind: str, subject: str, expected: Any, actual: Any, msg: str | None = None) -> None:
        mm.append(Mismatch(kind, subject, expected, actual, msg or f"expected {expected}, got {actual}"))

    # sequence settings
    if (snapshot.width, snapshot.height) != (spec.width, spec.height):
        add("sequence", "size", [spec.width, spec.height], [snapshot.width, snapshot.height])
    if snapshot.fps != spec.fps:
        add("sequence", "fps", str(spec.fps), str(snapshot.fps))
    if abs(snapshot.duration_frames - spec.end_frame) > tol:
        add("sequence", "duration_frames", spec.end_frame, snapshot.duration_frames)
    for t in spec.tracks:
        if snapshot.track(t) is None:
            add("track", t, "present", "missing")

    media_rows = project.media_by_item() if project else {}
    if media_map is None and project is not None:
        media_map = MediaMap()
        for r in project.media:
            if r.path:
                media_map.add(r.path, r.item)

    claimed: set[int] = set()

    def want_item(path: str) -> int | None:
        return media_map.get(path) if media_map is not None else None

    def pick(track: str, path: str, expected_start: int, *, name: str | None = None, prefer: int | None = None) -> ClipSnapshot | None:
        t = snapshot.track(track)
        if t is None:
            return None
        item = want_item(path) if path else None
        cands = [c for c in t.clips if c.clip_id not in claimed
                 and ((item is not None and c.item == item) or (item is None and name is not None and c.name == name))]
        if prefer is not None:
            pref = [c for c in cands if c.clip_id == prefer]
            if pref:
                return pref[0]
        if not cands:
            return None
        return min(cands, key=lambda c: (abs(c.start_frame - expected_start), c.clip_id))

    def check_clip(who: str, p_start: int, p_dur: int, c: ClipSnapshot, src_in: Fraction | None, src_out: Fraction | None,
                   gain: float | None) -> None:
        if abs(c.start_frame - p_start) > tol:
            add("start", who, p_start, c.start_frame)
        if abs(c.duration_frames - p_dur) > tol:
            add("duration", who, p_dur, c.duration_frames)
        if src_in is not None and abs(c.source_in_seconds - src_in) > tol_s:
            add("source_in", who, float(src_in), float(c.source_in_seconds))
        if src_out is not None and abs(c.source_out_seconds - src_out) > tol_s:
            add("source_out", who, float(src_out), float(c.source_out_seconds))
        if gain is not None and abs(c.gain_db - gain) > gain_tolerance_db:
            add("gain", who, gain, c.gain_db)

    from trip_vlog.filmcraft.timeline_spec import Placement  # noqa: F401  (type context)
    expected_order: dict[str, list[str]] = {}
    actual_primary: dict[str, ClipSnapshot] = {}
    n_clips = n_fit = n_duck = 0
    for p in sorted(spec.placements, key=lambda p: (p.track, p.timeline_start_frame)):
        who = p.clip_instance_id
        n_clips += 1
        expected_order.setdefault(p.track, []).append(who)
        c = pick(p.track, p.source_path, p.timeline_start_frame, name=os.path.basename(p.source_path))
        if c is None:
            add("missing_clip", who, f"{p.track}@{p.timeline_start_frame}", "not found",
                f"no clip for {os.path.basename(p.source_path)} on {p.track}")
            continue
        claimed.add(c.clip_id)
        actual_primary[who] = c
        sin = Fraction(str(p.source_in_seconds)) if p.media_kind != "image" else None
        sout = Fraction(str(p.source_out_seconds)) if (p.media_kind != "image" and p.source_out_seconds is not None) else None
        gain = (p.audio_gain_db if p.audio_gain_db is not None else 0.0) if p.media_kind == "audio" else None
        check_clip(who, p.timeline_start_frame, p.duration_frames, c, sin, sout, gain)
        n_fit += _check_framing(add, spec, p, c)
        if p.media_kind == "audio" and (p.gain_keys or c.volume_keyframes):
            n_duck += 1
            if c.volume_keyframes != len(p.gain_keys):
                add("ducking", who, len(p.gain_keys), c.volume_keyframes,
                    f"{who}: {len(p.gain_keys)} volume keyframe(s) expected, {c.volume_keyframes} read back")
        audio_ref: ClipSnapshot | None = None
        if p.media_kind == "video" and p.include_linked_audio:
            at = p.audio_track or ""
            a = pick(at, p.source_path, p.timeline_start_frame, name=os.path.basename(p.source_path), prefer=c.link)
            if a is None:
                add("missing_audio", who, at, "not found", f"linked audio of {who} missing on {at}")
            else:
                claimed.add(a.clip_id)
                audio_ref = a
                check_clip(f"{who}/audio", p.timeline_start_frame, p.duration_frames, a, sin, sout,
                           p.audio_gain_db if p.audio_gain_db is not None else 0.0)
        rep.clip_map.refs[who] = ClipRef(who, p.track, c.clip_id, audio_ref.clip_id if audio_ref else None,
                                         audio_ref.track if audio_ref else None)
        # media online + original path
        item = c.item
        if media_rows:
            row = media_rows.get(item) if item is not None else None
            if row is None:
                add("media", who, "known media item", item, f"{who}: clip refers to item {item}, which media.status does not list")
            else:
                if row.status != "online":
                    add("media_offline", who, "online", row.status, f"{who}: media is {row.status}")
                if row.path is None or canon_path(row.path) != canon_path(p.source_path):
                    add("media_path", who, p.source_path, row.path)
        else:
            rep.skipped.append("media online/path (no media.status data)") if "media online/path (no media.status data)" not in rep.skipped else None
    rep.checked["clips"] = n_clips
    if n_fit:
        rep.checked["framing"] = n_fit
    if n_duck:
        rep.checked["ducking"] = n_duck

    # order within each track
    for track, ids in expected_order.items():
        actual = [i for i, _ in sorted(((i, c) for i, c in actual_primary.items() if c.track == track), key=lambda ic: (ic[1].start_ticks, ic[1].clip_id))]
        if actual != ids and len(actual) == len(ids):
            add("order", track, ids, actual)

    # transitions
    trs = snapshot.transitions
    used_tr: set[int] = set()
    n_tr = 0
    for p in spec.placements:
        if p.transition_in.type != "dissolve":
            continue
        n_tr += 1
        c = actual_primary.get(p.clip_instance_id)
        if c is None:
            continue
        match = next((i for i, tr in enumerate(trs) if i not in used_tr and tr.to_clip == c.clip_id), None)
        if match is None:
            add("transition", p.clip_instance_id, f"dissolve {p.transition_in.frames}f", "none")
            continue
        used_tr.add(match)
        tr = trs[match]
        if tr.transition_id is not None:
            rep.clip_map.transitions[p.clip_instance_id] = tr.transition_id
        got = snapshot.timebase.ticks_to_frames(tr.duration_ticks)
        if abs(got - p.transition_in.frames) > tol:
            add("transition", p.clip_instance_id, p.transition_in.frames, got, f"dissolve length expected {p.transition_in.frames}f, got {got}f")
        if tr.effect != "cross_dissolve":
            add("transition", p.clip_instance_id, "cross_dissolve", tr.effect)
    for i, tr in enumerate(trs):
        if i not in used_tr:
            add("unexpected_transition", f"{tr.track}:{tr.effect}", "none", f"{tr.effect} {tr.duration_ticks} ticks")
    rep.checked["transitions"] = n_tr

    # titles
    for k, t in enumerate(spec.titles):
        tr_ = snapshot.track(t.track)
        cands = [c for c in (tr_.clips if tr_ else ()) if c.clip_id not in claimed and c.name == t.text]
        if not cands:
            add("missing_title", f"titles[{k}]", t.text, "not found")
            continue
        c = min(cands, key=lambda c: abs(c.start_frame - t.start_frame))
        claimed.add(c.clip_id)
        rep.clip_map.titles.append(c.clip_id)
        check_clip(f"titles[{k}]", t.start_frame, t.duration_frames, c, None, None, None)
    rep.checked["titles"] = len(spec.titles)

    # markers
    unclaimed_m = list(snapshot.markers)
    for k, m in enumerate(spec.markers):
        hit = next((a for a in unclaimed_m if a.name == m.name
                    and abs(snapshot.timebase.ticks_to_frames(a.start_ticks) - m.frame) <= tol), None)
        if hit is None:
            add("marker", f"markers[{k}]", {"name": m.name, "frame": m.frame}, "not found")
            continue
        unclaimed_m.remove(hit)
        if hit.marker_id is not None:
            rep.clip_map.markers.append(hit.marker_id)
        if m.comment and hit.comment != m.comment:
            add("marker", f"markers[{k}]", m.comment, hit.comment, "marker comment differs")
    for a in unclaimed_m:
        add("unexpected_marker", a.name, "none", snapshot.timebase.ticks_to_frames(a.start_ticks))
    rep.checked["markers"] = len(spec.markers)

    # anything on the timeline the spec does not account for (duplicates, leftovers)
    for c in snapshot.clips():
        if c.clip_id not in claimed:
            add("unexpected_clip", c.name or str(c.clip_id), "absent", f"{c.track}@{c.start_frame}+{c.duration_frames}",
                f"unexpected clip '{c.name}' (id {c.clip_id}) on {c.track} at frame {c.start_frame}")
    return rep
