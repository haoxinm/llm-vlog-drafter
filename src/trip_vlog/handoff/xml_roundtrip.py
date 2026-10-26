"""Compare an exported FCP7 XML with what was sent to FilmCraft, at frame precision (DESIGN §15.3, §20 Phase 5).

``expected_from_spec`` turns the (effective) ``TimelineSpec`` - the edit plan as mapped for FilmCraft, with dissolves the
engine could not place already downgraded - into the clips, titles, transitions and markers the XML must contain.
``compare_timeline_xml`` pairs every expected item with an XML item by track and identity (media path, or title text),
then compares record start/end, source in/out (in sequence frames), gain and path; leftovers on either side are reported.
Every mismatch carries the ``feature`` it belongs to, so the compatibility report can mark just that feature.

Pure functions over plain data: no FilmCraft, no Resolve, no filesystem access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from trip_vlog.filmcraft.timeline_spec import TimelineSpec, fit_scale_percent
from trip_vlog.handoff.fcp7_xml import Fcp7Timeline, XmlClip
from trip_vlog.resolve.paths import paths_equal

GAIN_TOLERANCE_DB = 0.25
SCALE_TOLERANCE = 0.5


def centred_transition_span(cut_frame: int, frames: int) -> tuple[int, int]:
    """(start, end) a centred dissolve of ``frames`` frames on ``cut_frame`` occupies in the FCP7 XML. An odd length cannot be centred
    exactly; FilmCraft puts the extra frame before the cut (measured on its real export: 15 frames at 105 -> 97..112, 3 -> 103..106)."""
    start = cut_frame - (frames + 1) // 2
    return start, start + frames


def transition_cut_frame(start: int, end: int) -> int:
    """The cut a transition written by ``centred_transition_span`` is centred on."""
    return start + (end - start + 1) // 2      # same rule as XmlTransition.cut_frame

# feature ids used by the compatibility report
F_SETTINGS, F_CUTS, F_DISSOLVES, F_STILLS, F_OVERLAYS, F_TITLES = "timeline_settings", "cuts", "dissolves", "stills", "overlays_v2", "titles"
F_MARKERS, F_GAIN, F_MUTE, F_MUSIC, F_AUDIO, F_SPEED = "markers", "audio_gain", "audio_mute", "music_track", "audio_clips", "speed_changes"
F_FRAMING, F_DUCKING = "framing_fit", "music_ducking"
KEYFRAME_TOLERANCE_FRAMES = 1      # a keyframe time is a source frame rounded to the sequence rate; one frame either way is the same keyframe


@dataclass(frozen=True)
class ExpectedClip:
    clip_instance_id: str
    feature: str
    kind: str                       # video | audio
    track: int                      # 1-based within kind
    start: int
    end: int
    source_in: int | None           # sequence frames (rounded); None for stills and titles
    source_out: int | None
    path: str | None
    text: str | None = None         # title text
    gain_db: float | None = None    # constant gain (audio items)
    source_in_exact: Fraction | None = None    # the same, unrounded: an exact half frame accepts either rounding
    source_out_exact: Fraction | None = None
    scale: float | None = None      # expected Basic Motion scale (percent) of a video item scaled to fit the frame
    gain_keys: tuple[tuple[int, float], ...] = ()   # expected Audio Levels keyframes: (source frame at the sequence rate, total dB)

    @property
    def is_title(self) -> bool:
        return self.text is not None

    @property
    def track_name(self) -> str:
        return f"{'V' if self.kind == 'video' else 'A'}{self.track}"


@dataclass(frozen=True)
class ExpectedTransition:
    clip_instance_id: str
    track: int
    cut_frame: int
    frames: int


@dataclass(frozen=True)
class ExpectedMarker:
    frame: int
    name: str
    comment: str


@dataclass
class ExpectedTimeline:
    fps: Fraction
    width: int
    height: int
    duration_frames: int
    clips: list[ExpectedClip] = field(default_factory=list)
    transitions: list[ExpectedTransition] = field(default_factory=list)
    markers: list[ExpectedMarker] = field(default_factory=list)
    muted_video_paths: set[tuple[str, int]] = field(default_factory=set)   # (path, start) of video clips placed without their audio


def _fit(spec: TimelineSpec, p: Any) -> float | None:
    return None if p.fit_source_size is None else fit_scale_percent(spec.width, spec.height, *p.fit_source_size)


def _track_no(track: str) -> int:
    return int(track[1:])


def expected_from_spec(spec: TimelineSpec) -> ExpectedTimeline:
    fps = Fraction(spec.fps)
    exp = ExpectedTimeline(fps, spec.width, spec.height, spec.end_frame)
    exact = lambda sec: Fraction(sec).limit_denominator(10 ** 9) * fps if sec is not None else None
    frames = lambda sec: int(round(exact(sec))) if sec is not None else None
    for p in spec.placements:
        vtrack = _track_no(p.track)
        sin, sout = (frames(p.source_in_seconds), frames(p.source_out_seconds)) if p.media_kind != "image" else (None, None)
        xin, xout = (exact(p.source_in_seconds), exact(p.source_out_seconds)) if p.media_kind != "image" else (None, None)
        if p.media_kind == "video":
            feat = F_CUTS if p.track == "V1" else F_OVERLAYS
            exp.clips.append(ExpectedClip(p.clip_instance_id, feat, "video", vtrack, p.timeline_start_frame, p.end_frame, sin, sout, p.source_path,
                                          source_in_exact=xin, source_out_exact=xout, scale=_fit(spec, p)))
        elif p.media_kind == "image":
            feat = F_STILLS if p.track == "V1" else F_OVERLAYS
            exp.clips.append(ExpectedClip(p.clip_instance_id, feat, "video", vtrack, p.timeline_start_frame, p.end_frame, None, None, p.source_path,
                                          scale=_fit(spec, p)))
        else:
            feat = F_MUSIC if p.track == "A2" else F_AUDIO
            exp.clips.append(ExpectedClip(p.clip_instance_id, feat, "audio", vtrack, p.timeline_start_frame, p.end_frame, sin, sout,
                                          p.source_path, gain_db=p.audio_gain_db or 0.0, source_in_exact=xin, source_out_exact=xout,
                                          gain_keys=tuple((int(round(Fraction(p.source_in_seconds).limit_denominator(10 ** 9) * fps)) + k.frame - p.timeline_start_frame,
                                                           (p.audio_gain_db or 0.0) + k.db) for k in p.gain_keys)))
        if p.media_kind == "video":
            if p.include_linked_audio and p.audio_track:
                exp.clips.append(ExpectedClip(p.clip_instance_id, F_CUTS if p.track == "V1" else F_OVERLAYS, "audio", _track_no(p.audio_track),
                                              p.timeline_start_frame, p.end_frame, sin, sout, p.source_path, gain_db=p.audio_gain_db or 0.0,
                                              source_in_exact=xin, source_out_exact=xout))
            else:
                exp.muted_video_paths.add((p.source_path, p.timeline_start_frame))
        if p.transition_in.type == "dissolve" and p.transition_in.frames > 0 and p.media_kind in ("video", "image"):
            exp.transitions.append(ExpectedTransition(p.clip_instance_id, vtrack, p.timeline_start_frame, p.transition_in.frames))
    for t in spec.titles:
        exp.clips.append(ExpectedClip(f"title:{t.text[:24]}", F_TITLES, "video", _track_no(t.track), t.start_frame, t.end_frame, None, None, None,
                                      text=t.text.split("\n")[0]))
    exp.markers = [ExpectedMarker(m.frame, m.name, m.comment) for m in spec.markers]
    return exp


@dataclass(frozen=True)
class Mismatch:
    feature: str
    ref: str
    kind: str          # missing | unexpected | record | source | path | gain | track | settings | transition | marker
    expected: Any = None
    actual: Any = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "ref": self.ref, "kind": self.kind, "expected": self.expected, "actual": self.actual,
                "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.feature}/{self.ref}: {self.detail}"


@dataclass
class RoundTrip:
    mismatches: list[Mismatch] = field(default_factory=list)
    expected: dict[str, int] = field(default_factory=dict)    # feature -> expected items
    matched: dict[str, int] = field(default_factory=dict)     # feature -> items found in the XML with every compared field equal
    clips: list[dict[str, Any]] = field(default_factory=list)
    tolerance_frames: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def for_feature(self, feature: str) -> list[Mismatch]:
        return [m for m in self.mismatches if m.feature == feature]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tolerance_frames": self.tolerance_frames, "expected": dict(self.expected), "matched": dict(self.matched),
                "mismatches": [m.to_dict() for m in self.mismatches], "clips": self.clips}


def _near(a: int | float | None, b: int | float | None, tol: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _near_source(actual: Fraction | int | None, e_int: int | None, e_exact: Fraction | None, tol: float) -> bool:
    """Source frame check against the unrounded expected value: an exact half frame accepts either rounding, anything else only the
    nearest frame (plus ``tol``)."""
    if actual is None or e_int is None:
        return False
    exact = e_exact if e_exact is not None else Fraction(e_int)
    slack = Fraction(1, 2) if exact.denominator != 1 else Fraction(0)
    return abs(Fraction(actual) - exact) <= slack + Fraction(tol).limit_denominator(1000)


def _identity_ok(e: ExpectedClip, x: XmlClip, fold_case: bool | None) -> bool:
    if e.is_title:
        return x.is_generator and (x.name == e.text or x.text == e.text)
    return (not x.is_generator) and x.path is not None and e.path is not None and paths_equal(e.path, x.path, fold_case=fold_case)


def compare_timeline_xml(xml: Fcp7Timeline, exp: ExpectedTimeline, *, tolerance_frames: int = 0, fold_case: bool | None = None,
                         keyframes_carried: bool = True) -> RoundTrip:
    """Compare every clip, title, transition and marker of ``exp`` with ``xml``. ``keyframes_carried=False`` (an XML generated from the FilmCraft read-back, which cannot read keyframe values) leaves the Audio Levels keyframes out of the comparison. ``tolerance_frames`` (default 0: exact) applies to every frame comparison: sequence duration, record and source frames, transition start/end, markers."""
    rt = RoundTrip(tolerance_frames=tolerance_frames)
    tol = tolerance_frames
    add = lambda *a, **k: rt.mismatches.append(Mismatch(*a, **k))
    bump = lambda d, f, n=1: d.__setitem__(f, d.get(f, 0) + n)

    # sequence
    bad = []
    if xml.fps != exp.fps:
        bad.append(f"frame rate {xml.fps} (expected {exp.fps})")
    if (xml.width, xml.height) != (exp.width, exp.height):
        bad.append(f"size {xml.width}x{xml.height} (expected {exp.width}x{exp.height})")
    if not _near(xml.duration, exp.duration_frames, tol):
        bad.append(f"duration {xml.duration} frames (expected {exp.duration_frames})")
    bump(rt.expected, F_SETTINGS)
    if bad:
        add(F_SETTINGS, "sequence", "settings", detail="; ".join(bad))
    else:
        bump(rt.matched, F_SETTINGS)

    # clips, per track
    groups: dict[tuple[str, int], list[ExpectedClip]] = {}
    for e in exp.clips:
        groups.setdefault((e.kind, e.track), []).append(e)
        bump(rt.expected, e.feature)
        if e.kind == "audio" and e.gain_db and abs(e.gain_db) > 1e-9:
            bump(rt.expected, F_GAIN)
        if e.scale is not None:
            bump(rt.expected, F_FRAMING)
        if e.gain_keys and keyframes_carried:
            bump(rt.expected, F_DUCKING)
    used: set[int] = set()
    for (kind, trk), items in sorted(groups.items()):
        xs = xml.track_clips(kind, trk)
        for e in sorted(items, key=lambda c: (c.start, c.clip_instance_id)):
            cands = [(i, x) for i, x in enumerate(xs) if id(x) not in used and x.enabled and _identity_ok(e, x, fold_case)]
            # An exact path match wins over one that only matches after case folding (A.mov vs a.mov on macOS).
            exact = [c for c in cands if e.is_title or _identity_ok(e, c[1], False)]
            cands = exact or cands
            label = f"{e.track_name} {e.clip_instance_id}" + (f" ({e.text})" if e.is_title else "")
            if not cands:
                add(e.feature, label, "missing", {"start": e.start, "end": e.end, "path": e.path or e.text}, None,
                    f"no {'title' if e.is_title else 'clip'} on {e.track_name} at frame {e.start}"
                    + ("" if e.is_title else f" for {e.path}"))
                rt.clips.append({"clip_instance_id": e.clip_instance_id, "track": e.track_name, "matched": False,
                                 "record": [e.start, e.end], "xml_record": None})
                continue
            _, x = min(cands, key=lambda ix: abs((ix[1].start if ix[1].start is not None else 10 ** 9) - e.start))
            used.add(id(x))
            problems = 0
            if not (_near(x.start, e.start, tol) and _near(x.end, e.end, tol)):
                problems += 1
                add(e.feature, label, "record", [e.start, e.end], [x.start, x.end], f"record {x.start}-{x.end}, expected {e.start}-{e.end}")
            if e.source_in is not None:
                sin, sout = x.source_in_in(exp.fps), x.source_out_in(exp.fps)
                if not (_near_source(sin, e.source_in, e.source_in_exact, tol) and _near_source(sout, e.source_out, e.source_out_exact, tol)):
                    problems += 1
                    add(e.feature, label, "source", [e.source_in, e.source_out], [_f(sin), _f(sout)],
                        f"source {_f(sin)}-{_f(sout)}, expected {e.source_in}-{e.source_out}")
            if e.kind == "audio" and e.gain_db is not None:
                actual_gain = 0.0 if x.gain_db is None else x.gain_db
                if abs(actual_gain - e.gain_db) > GAIN_TOLERANCE_DB:
                    problems += 1
                    add(F_GAIN, label, "gain", e.gain_db, _round(actual_gain),
                        f"gain {actual_gain:.2f} dB, expected {e.gain_db:.2f} dB")
            if e.scale is not None:
                if x.scale is None or abs(x.scale - e.scale) > SCALE_TOLERANCE:
                    problems += 1
                    add(F_FRAMING, label, "scale", round(e.scale, 2), x.scale, f"scale {x.scale}, expected {round(e.scale, 2)} (fit to frame)")
                else:
                    bump(rt.matched, F_FRAMING)
            if e.gain_keys and keyframes_carried:
                got = list(x.gain_keys)
                ok = len(got) == len(e.gain_keys) and all(
                    abs(gw - ew) <= max(tol, KEYFRAME_TOLERANCE_FRAMES) and abs(gd - ed) <= GAIN_TOLERANCE_DB for (gw, gd), (ew, ed) in zip(got, e.gain_keys))
                if not ok:
                    problems += 1
                    add(F_DUCKING, label, "keyframes", [[w, round(d, 2)] for w, d in e.gain_keys], [[w, round(d, 2)] for w, d in got],
                        f"{len(got)} volume keyframe(s) in the XML, expected {len(e.gain_keys)} (ducking envelope)")
                else:
                    bump(rt.matched, F_DUCKING)
            if not problems:
                bump(rt.matched, e.feature)
                if e.kind == "audio" and e.gain_db and abs(e.gain_db) > 1e-9:
                    bump(rt.matched, F_GAIN)
            rt.clips.append({"clip_instance_id": e.clip_instance_id, "track": e.track_name, "matched": True, "ok": problems == 0,
                             "record": [e.start, e.end], "xml_record": [x.start, x.end],
                             "source": None if e.source_in is None else [e.source_in, e.source_out],
                             "xml_source": None if e.source_in is None else [_f(x.source_in_in(exp.fps)), _f(x.source_out_in(exp.fps))]})
    for x in xml.clips:
        if id(x) in used or not x.enabled:
            continue
        if x.has_speed:
            feat = F_SPEED
        elif x.kind == "audio" and x.path and any(paths_equal(p, x.path, fold_case=fold_case) and abs(s - (x.start or 0)) <= tol
                                                  for p, s in exp.muted_video_paths):
            feat = F_MUTE
        elif x.is_generator:
            feat = F_TITLES
        elif x.kind == "audio":
            feat = F_AUDIO
        else:
            feat = F_OVERLAYS if x.track > 1 else F_CUTS
        add(feat, f"{x.track_name} {x.name}", "unexpected", None, {"start": x.start, "end": x.end, "path": x.path},
            f"{x.track_name} has an item '{x.name}' at {x.start}-{x.end} that the plan does not contain")
    for x in xml.clips:
        if x.has_speed and id(x) in used:
            add(F_SPEED, f"{x.track_name} {x.name}", "unexpected", None, None, "the item has a speed / time-remap filter; the plan has none")

    # transitions (video only; audio fades are not generated)
    xts = [t for t in xml.transitions if t.kind == "video"]
    left = list(xts)
    for et in exp.transitions:
        bump(rt.expected, F_DISSOLVES)
        es, ee = centred_transition_span(et.cut_frame, et.frames)
        hit = next((t for t in left if t.track == et.track and t.start is not None and t.end is not None
                    and abs(t.start - es) <= tol and abs(t.end - ee) <= tol), None)
        if hit is None:
            add(F_DISSOLVES, f"V{et.track} {et.clip_instance_id}", "transition", {"cut_frame": et.cut_frame, "frames": et.frames, "start": es, "end": ee},
                None, f"no {et.frames}-frame transition at {es}-{ee} (centred on frame {et.cut_frame}) on V{et.track}")
        else:
            left.remove(hit)
            bump(rt.matched, F_DISSOLVES)
    for t in left:
        add(F_DISSOLVES, f"{t.track_name}", "transition", None, {"start": t.start, "end": t.end},
            f"{t.track_name} has a transition at {t.start}-{t.end} ({t.effect}) that the plan does not contain")
    for t in xml.transitions:
        if t.kind == "audio":
            add(F_AUDIO, t.track_name, "transition", None, {"start": t.start, "end": t.end},
                f"{t.track_name} has an audio transition at {t.start}-{t.end} ({t.effect}); none was planned")

    # hard cuts on V1: contiguous neighbours without a transition between them
    exp_bounds = _hard_cuts([(c.start, c.end) for c in exp.clips if c.kind == "video" and c.track == 1 and not c.is_title],
                            [t.cut_frame for t in exp.transitions if t.track == 1], tol)
    xml_bounds = _hard_cuts([(c.start, c.end) for c in xml.track_clips("video", 1) if not c.is_generator and c.start is not None and c.end is not None],
                            [transition_cut_frame(t.start, t.end) for t in xts if t.track == 1 and t.start is not None and t.end is not None], tol)
    rt.expected[F_CUTS + "_boundaries"] = exp_bounds
    rt.matched[F_CUTS + "_boundaries"] = xml_bounds

    # markers
    xm = list(xml.markers)
    for m in exp.markers:
        bump(rt.expected, F_MARKERS)
        hit = next((x for x in xm if x.name == m.name and _near(x.frame, m.frame, tol)), None)
        if hit is None:
            add(F_MARKERS, m.name, "marker", {"frame": m.frame, "name": m.name}, None, f"marker '{m.name}' at frame {m.frame} is not in the XML")
        else:
            xm.remove(hit)
            bump(rt.matched, F_MARKERS)
    for x in xm:
        add(F_MARKERS, x.name, "marker", None, {"frame": x.frame, "name": x.name}, f"the XML has a marker '{x.name}' at frame {x.frame} that the plan does not contain")
    return rt


def _hard_cuts(spans: list[tuple[int | None, int | None]], transition_cuts: list[int], tol: float) -> int:
    ordered = sorted((s, e) for s, e in spans if s is not None and e is not None)
    n = 0
    for (_, e1), (s2, _) in zip(ordered, ordered[1:]):
        if abs(s2 - e1) <= tol and not any(abs(c - s2) <= tol for c in transition_cuts):
            n += 1
    return n


def _f(v: Fraction | None) -> float | int | None:
    if v is None:
        return None
    return int(v) if v.denominator == 1 else round(float(v), 2)


def _round(v: float) -> float:
    return round(v, 2)
