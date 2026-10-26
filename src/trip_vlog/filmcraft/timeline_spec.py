"""Low-level, FilmCraft-agnostic input of the adapter: a fully resolved timeline in whole frames.

The edit-plan -> TimelineSpec mapping lives elsewhere. Everything here is plain data plus pure validation:
frames are integers, source ranges are seconds (converted to ticks only inside the adapter), tracks are
named ``V1..Vn`` / ``A1..An``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Literal, Mapping, Sequence

from trip_vlog.filmcraft.errors import SpecValidationError
from trip_vlog.filmcraft.timebase import Timebase, normalize_fps, to_fraction

SPEC_SCHEMA_VERSION = 1
MediaKind = Literal["video", "image", "audio"]
MEDIA_KINDS = ("video", "image", "audio")
TRACK_RE = re.compile(r"^([VA])([1-9][0-9]{0,2})$")
MAX_TITLE_CHARS = 500
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def parse_track(track: str) -> tuple[str, int]:
    """'V2' -> ('V', 2). Raises ValueError for anything else."""
    m = TRACK_RE.match(track) if isinstance(track, str) else None
    if not m:
        raise ValueError(f"track must look like 'V1' or 'A2', got {track!r}")
    return m[1], int(m[2])


@dataclass(frozen=True)
class TransitionIn:
    type: str = "cut"  # "cut" | "dissolve"
    frames: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "frames": self.frames}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "TransitionIn":
        d = d or {}
        return cls(type=d.get("type", "cut"), frames=int(d.get("frames", 0)))


@dataclass(frozen=True)
class GainKey:
    """One keyframe of a clip's Volume level (``volume.level``, dB, added to the clip gain) at a sequence frame."""
    frame: int
    db: float

    def to_dict(self) -> dict[str, Any]:
        return {"frame": self.frame, "db": self.db}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GainKey":
        return cls(frame=int(d["frame"]), db=float(d["db"]))


def fit_scale_percent(seq_w: int, seq_h: int, src_w: int, src_h: int) -> float:
    """Scale (percent) that fits a ``src_w`` x ``src_h`` picture inside the sequence frame without cropping (FilmCraft ``clip.fitToFrame``)."""
    return min(seq_w / src_w, seq_h / src_h) * 100.0


@dataclass(frozen=True)
class Placement:
    clip_instance_id: str
    source_path: str  # absolute path of the original asset
    media_kind: str  # video | image | audio
    track: str  # "V1", "V2", "A1", "A2" ...
    timeline_start_frame: int
    duration_frames: int
    source_in_seconds: float = 0.0
    source_out_seconds: float | None = None  # None for stills
    include_linked_audio: bool = True  # video only: place the clip's own audio too (False = video-only)
    audio_gain_db: float | None = None
    transition_in: TransitionIn = field(default_factory=TransitionIn)
    linked_audio_track: str | None = None  # default: the A track with the same index as the video track
    fit_source_size: tuple[int, int] | None = None  # video/image: display size of the source; set = scale it to fit the frame (clip.fitToFrame)
    gain_keys: tuple[GainKey, ...] = ()  # audio: Volume level keyframes (ducking), sequence frames, strictly increasing

    @property
    def end_frame(self) -> int:
        return self.timeline_start_frame + self.duration_frames

    @property
    def has_audio(self) -> bool:
        return self.media_kind == "audio" or (self.media_kind == "video" and self.include_linked_audio)

    @property
    def audio_track(self) -> str | None:
        """Track carrying this placement's audio (None if it has none)."""
        if self.media_kind == "audio":
            return self.track
        if self.media_kind == "video" and self.include_linked_audio:
            if self.linked_audio_track:
                return self.linked_audio_track
            try:
                return f"A{parse_track(self.track)[1]}"
            except ValueError:
                return None
        return None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "clip_instance_id": self.clip_instance_id, "source_path": self.source_path,
            "media_kind": self.media_kind, "track": self.track,
            "timeline_start_frame": self.timeline_start_frame, "duration_frames": self.duration_frames,
            "source_in_seconds": self.source_in_seconds, "source_out_seconds": self.source_out_seconds,
            "include_linked_audio": self.include_linked_audio, "audio_gain_db": self.audio_gain_db,
            "transition_in": self.transition_in.to_dict(), "linked_audio_track": self.linked_audio_track,
        }
        # only written when used, so the hash of a spec without framing or ducking is the one it always had
        if self.fit_source_size is not None:
            d["fit_source_size"] = list(self.fit_source_size)
        if self.gain_keys:
            d["gain_keys"] = [k.to_dict() for k in self.gain_keys]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Placement":
        return cls(
            clip_instance_id=d["clip_instance_id"], source_path=d["source_path"], media_kind=d["media_kind"],
            track=d["track"], timeline_start_frame=int(d["timeline_start_frame"]),
            duration_frames=int(d["duration_frames"]), source_in_seconds=d.get("source_in_seconds", 0.0),
            source_out_seconds=d.get("source_out_seconds"), include_linked_audio=bool(d.get("include_linked_audio", True)),
            audio_gain_db=d.get("audio_gain_db"), transition_in=TransitionIn.from_dict(d.get("transition_in")),
            linked_audio_track=d.get("linked_audio_track"),
            fit_source_size=tuple(d["fit_source_size"]) if d.get("fit_source_size") else None,
            gain_keys=tuple(GainKey.from_dict(k) for k in d.get("gain_keys", ())),
        )


@dataclass(frozen=True)
class MarkerSpec:
    frame: int
    name: str
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"frame": self.frame, "name": self.name, "comment": self.comment}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MarkerSpec":
        return cls(frame=int(d["frame"]), name=d["name"], comment=d.get("comment", ""))


@dataclass(frozen=True)
class TitleSpec:
    text: str
    start_frame: int
    duration_frames: int
    track: str = "V2"
    size: int | None = None  # px; default derived from sequence height
    position: tuple[int, int] | None = None  # px; default lower-left third

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.duration_frames

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start_frame": self.start_frame, "duration_frames": self.duration_frames,
                "track": self.track, "size": self.size, "position": list(self.position) if self.position else None}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TitleSpec":
        pos = d.get("position")
        return cls(text=d["text"], start_frame=int(d["start_frame"]), duration_frames=int(d["duration_frames"]),
                   track=d.get("track", "V2"), size=d.get("size"), position=tuple(pos) if pos else None)


@dataclass(frozen=True)
class TimelineSpec:
    sequence_name: str
    width: int
    height: int
    fps: Fraction
    tracks: tuple[str, ...]  # tracks needed, e.g. ("V1", "V2", "A1", "A2")
    placements: tuple[Placement, ...] = ()
    markers: tuple[MarkerSpec, ...] = ()
    titles: tuple[TitleSpec, ...] = ()

    def __post_init__(self) -> None:
        sset = object.__setattr__
        sset(self, "fps", normalize_fps(self.fps))
        sset(self, "tracks", tuple(self.tracks))
        sset(self, "placements", tuple(self.placements))
        sset(self, "markers", tuple(self.markers))
        sset(self, "titles", tuple(self.titles))

    # ---- derived ----
    @property
    def timebase(self) -> Timebase:
        return Timebase(self.fps)

    @property
    def video_track_count(self) -> int:
        return max((parse_track(t)[1] for t in self.tracks if t.startswith("V")), default=0)

    @property
    def audio_track_count(self) -> int:
        return max((parse_track(t)[1] for t in self.tracks if t.startswith("A")), default=0)

    @property
    def end_frame(self) -> int:
        ends = [p.end_frame for p in self.placements] + [t.end_frame for t in self.titles]
        return max(ends, default=0)

    @property
    def asset_paths(self) -> list[str]:
        """Distinct source paths, in first-use order."""
        return list(dict.fromkeys(p.source_path for p in self.placements))

    # ---- (de)serialisation ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SPEC_SCHEMA_VERSION, "sequence_name": self.sequence_name,
            "width": self.width, "height": self.height, "fps": f"{self.fps.numerator}/{self.fps.denominator}",
            "tracks": list(self.tracks), "placements": [p.to_dict() for p in self.placements],
            "markers": [m.to_dict() for m in self.markers], "titles": [t.to_dict() for t in self.titles],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TimelineSpec":
        return cls(
            sequence_name=d["sequence_name"], width=int(d["width"]), height=int(d["height"]), fps=to_fraction(d["fps"]),
            tracks=tuple(d["tracks"]), placements=tuple(Placement.from_dict(p) for p in d.get("placements", ())),
            markers=tuple(MarkerSpec.from_dict(m) for m in d.get("markers", ())),
            titles=tuple(TitleSpec.from_dict(t) for t in d.get("titles", ())),
        )

    def spec_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Pure validation
# ----------------------------------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_spec(spec: TimelineSpec) -> list[str]:
    """Every structural problem in ``spec`` (no filesystem or FilmCraft access). Empty list = valid."""
    issues: list[str] = []
    add = issues.append

    if not isinstance(spec.sequence_name, str) or not spec.sequence_name.strip():
        add("sequence_name must be a non-empty string")
    for name in ("width", "height"):
        v = getattr(spec, name)
        if not _is_int(v) or not (16 <= v <= 16384):
            add(f"{name} must be an integer between 16 and 16384, got {v!r}")
    if not (0 < spec.fps <= 240):
        add(f"fps must be in (0, 240], got {spec.fps}")

    # tracks
    tracks_ok = True
    seen_tracks: set[str] = set()
    for t in spec.tracks:
        try:
            parse_track(t)
        except ValueError as e:
            add(str(e))
            tracks_ok = False
            continue
        if t in seen_tracks:
            add(f"duplicate track {t}")
        seen_tracks.add(t)
    if tracks_ok:
        if "V1" not in seen_tracks:
            add("tracks must include V1")
        for kind, count in (("V", spec.video_track_count), ("A", spec.audio_track_count)):
            for i in range(1, count + 1):
                if f"{kind}{i}" not in seen_tracks:
                    add(f"tracks must be contiguous: {kind}{count} is listed but {kind}{i} is not")
    if not (_is_int(spec.video_track_count) and spec.video_track_count <= 32 and spec.audio_track_count <= 32):
        add("at most 32 video and 32 audio tracks are supported")

    ids: set[str] = set()
    occupancy: dict[str, list[tuple[int, int, str]]] = {}

    def occupy(track: str, start: int, end: int, who: str) -> None:
        occupancy.setdefault(track, []).append((start, end, who))

    tb = spec.timebase if spec.fps > 0 else None
    for i, p in enumerate(spec.placements):
        who = p.clip_instance_id if isinstance(p.clip_instance_id, str) and p.clip_instance_id else f"placements[{i}]"
        if not isinstance(p.clip_instance_id, str) or not p.clip_instance_id:
            add(f"placements[{i}]: clip_instance_id must be a non-empty string")
        elif p.clip_instance_id in ids:
            add(f"{who}: duplicate clip_instance_id")
        ids.add(str(p.clip_instance_id))
        if p.media_kind not in MEDIA_KINDS:
            add(f"{who}: media_kind must be one of {MEDIA_KINDS}, got {p.media_kind!r}")
            continue
        if not isinstance(p.source_path, str) or not p.source_path:
            add(f"{who}: source_path must be a non-empty string")
        if not _is_int(p.timeline_start_frame) or p.timeline_start_frame < 0:
            add(f"{who}: timeline_start_frame must be an integer >= 0, got {p.timeline_start_frame!r}")
            continue
        if not _is_int(p.duration_frames) or p.duration_frames <= 0:
            add(f"{who}: duration_frames must be an integer > 0, got {p.duration_frames!r}")
            continue
        try:
            kind, idx = parse_track(p.track)
        except ValueError as e:
            add(f"{who}: {e}")
            continue
        if p.track not in seen_tracks:
            add(f"{who}: track {p.track} is not in the spec's tracks")
        if p.media_kind in ("video", "image") and kind != "V":
            add(f"{who}: {p.media_kind} must be placed on a V track, not {p.track}")
        if p.media_kind == "audio" and kind != "A":
            add(f"{who}: audio must be placed on an A track, not {p.track}")

        # source range
        if p.media_kind == "image":
            if p.source_out_seconds is not None or p.source_in_seconds not in (0, 0.0):
                add(f"{who}: stills take no source range (source_in_seconds=0, source_out_seconds=None)")
        else:
            if not _is_num(p.source_in_seconds) or p.source_in_seconds < 0:
                add(f"{who}: source_in_seconds must be a finite number >= 0, got {p.source_in_seconds!r}")
            elif p.source_out_seconds is None or not _is_num(p.source_out_seconds):
                add(f"{who}: source_out_seconds is required for {p.media_kind}")
            elif p.source_out_seconds <= p.source_in_seconds:
                add(f"{who}: source_out_seconds must be greater than source_in_seconds")
            elif tb is not None:
                span = (to_fraction(p.source_out_seconds) - to_fraction(p.source_in_seconds)) * spec.fps
                if abs(span - p.duration_frames) > 1:
                    add(f"{who}: source range is {float(span):.2f} frames but duration_frames is {p.duration_frames} "
                        "(speed changes are not supported; they must agree within 1 frame)")

        # audio
        if p.audio_gain_db is not None:
            if not _is_num(p.audio_gain_db) or not (-96 <= p.audio_gain_db <= 24):
                add(f"{who}: audio_gain_db must be a finite number in [-96, 24], got {p.audio_gain_db!r}")
            if not p.has_audio:
                add(f"{who}: audio_gain_db given but the placement has no audio")
        if p.linked_audio_track is not None:
            if p.media_kind != "video" or not p.include_linked_audio:
                add(f"{who}: linked_audio_track only applies to a video placement with include_linked_audio")
            else:
                try:
                    if parse_track(p.linked_audio_track)[0] != "A":
                        add(f"{who}: linked_audio_track must be an A track")
                except ValueError as e:
                    add(f"{who}: {e}")
        at = p.audio_track
        if at is not None and at not in seen_tracks:
            add(f"{who}: audio goes to {at}, which is not in the spec's tracks")

        # framing and ducking
        if p.fit_source_size is not None:
            fs = p.fit_source_size
            if p.media_kind not in ("video", "image"):
                add(f"{who}: fit_source_size only applies to video and still placements")
            elif not (len(fs) == 2 and all(_is_int(v) and 1 <= v <= 65536 for v in fs)):
                add(f"{who}: fit_source_size must be (width, height) in whole pixels, got {fs!r}")
        if p.gain_keys:
            if p.media_kind != "audio":
                add(f"{who}: gain_keys only apply to a stand-alone audio placement")
            else:
                last = -1
                for k in p.gain_keys:
                    if not _is_int(k.frame) or not (p.timeline_start_frame <= k.frame < p.end_frame):
                        add(f"{who}: gain key at frame {k.frame!r} lies outside the clip [{p.timeline_start_frame}, {p.end_frame})")
                    elif k.frame <= last:
                        add(f"{who}: gain keys must be strictly increasing in frame (frame {k.frame})")
                    if not _is_num(k.db) or not (-96 <= k.db <= 15):
                        add(f"{who}: gain key level must be a finite number in [-96, 15] dB, got {k.db!r}")
                    last = k.frame if _is_int(k.frame) else last

        # transition
        ti = p.transition_in
        if ti.type not in ("cut", "dissolve"):
            add(f"{who}: transition_in.type must be 'cut' or 'dissolve', got {ti.type!r}")
        elif ti.type == "cut":
            if ti.frames != 0:
                add(f"{who}: a cut has frames=0, got {ti.frames}")
        else:
            if kind != "V":
                add(f"{who}: dissolves are only supported on video tracks")
            if not _is_int(ti.frames) or ti.frames < 2:
                add(f"{who}: dissolve needs frames >= 2, got {ti.frames!r}")

        occupy(p.track, p.timeline_start_frame, p.end_frame, who)
        if at is not None and p.media_kind == "video":
            occupy(at, p.timeline_start_frame, p.end_frame, f"{who} (linked audio)")

    # titles
    for i, t in enumerate(spec.titles):
        who = f"titles[{i}]"
        if not isinstance(t.text, str) or not t.text.strip():
            add(f"{who}: text must be a non-empty string")
        elif len(t.text) > MAX_TITLE_CHARS or _CONTROL.search(t.text):
            add(f"{who}: text too long or contains control characters")
        if not _is_int(t.start_frame) or t.start_frame < 0 or not _is_int(t.duration_frames) or t.duration_frames <= 0:
            add(f"{who}: start_frame must be >= 0 and duration_frames > 0")
            continue
        try:
            if parse_track(t.track)[0] != "V":
                add(f"{who}: titles go on a V track, not {t.track}")
        except ValueError as e:
            add(f"{who}: {e}")
            continue
        if t.track not in seen_tracks:
            add(f"{who}: track {t.track} is not in the spec's tracks")
        if t.size is not None and (not _is_int(t.size) or not (4 <= t.size <= 2000)):
            add(f"{who}: size must be an integer between 4 and 2000")
        if t.position is not None and (len(t.position) != 2 or not all(_is_num(c) for c in t.position)):
            add(f"{who}: position must be an (x, y) pair of numbers")
        occupy(t.track, t.start_frame, t.end_frame, f"{who} ('{t.text[:20]}')")

    # markers
    for i, m in enumerate(spec.markers):
        if not _is_int(m.frame) or m.frame < 0:
            add(f"markers[{i}]: frame must be an integer >= 0")
        if not isinstance(m.name, str) or not m.name.strip():
            add(f"markers[{i}]: name must be a non-empty string")

    # overlaps: FilmCraft overwrites silently, so overlaps are refused here
    for track, ivs in occupancy.items():
        ivs.sort()
        for (s1, e1, w1), (s2, e2, w2) in zip(ivs, ivs[1:]):
            if s2 < e1:
                add(f"overlap on {track}: {w1} [{s1},{e1}) and {w2} [{s2},{e2})")

    # dissolves need an adjacent predecessor on the same track
    by_track: dict[str, list[Placement]] = {}
    for p in spec.placements:
        by_track.setdefault(p.track, []).append(p)
    for track, ps in by_track.items():
        ps.sort(key=lambda p: p.timeline_start_frame if _is_int(p.timeline_start_frame) else 0)
        for prev, cur in zip([None, *ps], ps):
            ti = cur.transition_in
            if ti.type != "dissolve" or not _is_int(ti.frames):
                continue
            if prev is None or prev.end_frame != cur.timeline_start_frame:
                add(f"{cur.clip_instance_id}: dissolve needs a clip ending exactly where it starts on {track}")
                continue
            half = -(-ti.frames // 2)
            if half > prev.duration_frames or half > cur.duration_frames:
                add(f"{cur.clip_instance_id}: dissolve of {ti.frames} frames is longer than the adjacent clips allow")
    return issues


def ensure_valid(spec: TimelineSpec) -> None:
    issues = validate_spec(spec)
    if issues:
        raise SpecValidationError(issues)


@dataclass(frozen=True)
class HandleIssue:
    clip_instance_id: str
    message: str


def check_media_bounds(
    spec: TimelineSpec, durations: Mapping[str, Fraction | float | int], *, tolerance_frames: int = 1
) -> tuple[list[str], list[HandleIssue]]:
    """Checks that need the real asset durations (seconds, keyed by ``source_path``).

    Returns ``(errors, handle_issues)``. ``errors`` are hard failures (source range past the end of the
    media, unknown asset). ``handle_issues`` are dissolves that lack source handles on the outgoing or
    incoming clip - the caller decides whether to fail or downgrade them to cuts.
    """
    errors: list[str] = []
    handles: list[HandleIssue] = []
    tol = Fraction(tolerance_frames) / spec.fps
    dur_of: dict[str, Fraction] = {}
    for p in spec.placements:
        if p.source_path not in durations:
            errors.append(f"{p.clip_instance_id}: no duration known for {p.source_path}")
            continue
        dur_of[p.clip_instance_id] = to_fraction(durations[p.source_path])
        if p.media_kind in ("video", "audio") and p.source_out_seconds is not None:
            if to_fraction(p.source_out_seconds) > dur_of[p.clip_instance_id] + tol:
                errors.append(
                    f"{p.clip_instance_id}: source_out_seconds={p.source_out_seconds} is past the end of the media "
                    f"({float(dur_of[p.clip_instance_id]):.3f}s)")
    by_track: dict[str, list[Placement]] = {}
    for p in spec.placements:
        by_track.setdefault(p.track, []).append(p)
    for ps in by_track.values():
        ps.sort(key=lambda p: p.timeline_start_frame)
        for prev, cur in zip([None, *ps], ps):
            ti = cur.transition_in
            if ti.type != "dissolve" or prev is None:
                continue
            half = Fraction(-(-ti.frames // 2)) / spec.fps  # seconds of handle needed on each side
            if prev.media_kind == "video" and prev.clip_instance_id in dur_of and prev.source_out_seconds is not None:
                avail = dur_of[prev.clip_instance_id] - to_fraction(prev.source_out_seconds)
                if avail + tol < half:
                    handles.append(HandleIssue(cur.clip_instance_id, f"outgoing clip {prev.clip_instance_id} has only "
                                               f"{float(avail):.3f}s of media after its out point, needs {float(half):.3f}s"))
            if cur.media_kind == "video" and to_fraction(cur.source_in_seconds) + tol < half:
                handles.append(HandleIssue(cur.clip_instance_id, f"incoming clip has only {float(cur.source_in_seconds):.3f}s "
                                           f"of media before its in point, needs {float(half):.3f}s"))
    return errors, handles


def downgrade_dissolves(spec: TimelineSpec, clip_ids: Iterable[str]) -> TimelineSpec:
    """Return a copy of ``spec`` where the listed placements use a hard cut instead of a dissolve."""
    from dataclasses import replace

    wanted = set(clip_ids)
    new = tuple(replace(p, transition_in=TransitionIn("cut", 0)) if p.clip_instance_id in wanted else p
                for p in spec.placements)
    return replace(spec, placements=new)
