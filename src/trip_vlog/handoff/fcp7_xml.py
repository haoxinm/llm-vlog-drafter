"""Read an exported FCP7 XML (xmeml) timeline into plain data (DESIGN §15.3, §15.5).

Only what the handoff compatibility report compares is read: sequence rate/size/duration, clip items per track (record
start/end, source in/out, rate, media path, text of a title, constant gain), transition items and sequence markers.
Nothing here depends on FilmCraft or Resolve, and nothing touches the media. Path references are decoded to plain path text
(``file://localhost/C%3a/Users/me/a%20b.mp4`` -> ``C:/Users/me/a b.mp4``; ``file:///Users/me/Movies/a.mov`` -> ``/Users/me/Movies/a.mov``)
so a Windows-style and a macOS-style export can both be compared with the same code (see ``trip_vlog.resolve.paths``).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from trip_vlog.filmcraft.interchange import url_to_path


class XmlReadError(ValueError):
    pass


@dataclass(frozen=True)
class XmlClip:
    kind: str                 # "video" | "audio"
    track: int                # 1-based position of the <track> within its media kind
    index: int                # position among the items of that track
    item_id: str | None
    name: str
    start: int | None         # record frames of the sequence; None when the file says -1 and no transition defines it
    end: int | None
    source_in: int | None     # frames at ``rate``
    source_out: int | None
    rate: Fraction
    path: str | None          # decoded path text (None for generated items such as titles)
    is_generator: bool        # a title / generator: no media file behind it
    text: str | None = None   # Source Text of a title
    gain_db: float | None = None
    has_speed: bool = False
    enabled: bool = True
    start_derived: bool = False   # the file says -1 and ``start`` is the cut of the transition in front of the item
    end_derived: bool = False     # likewise for ``end`` and the transition behind it

    @property
    def track_name(self) -> str:
        return f"{'V' if self.kind == 'video' else 'A'}{self.track}"

    def source_in_in(self, fps: Fraction) -> Fraction | None:
        """Source in point in frames of ``fps`` (a clip item may carry its own rate)."""
        return None if self.source_in is None else Fraction(self.source_in) * fps / self.rate

    def source_out_in(self, fps: Fraction) -> Fraction | None:
        return None if self.source_out is None else Fraction(self.source_out) * fps / self.rate


@dataclass(frozen=True)
class XmlTransition:
    kind: str
    track: int
    start: int | None
    end: int | None
    alignment: str | None
    effect: str

    @property
    def track_name(self) -> str:
        return f"{'V' if self.kind == 'video' else 'A'}{self.track}"

    @property
    def frames(self) -> int | None:
        return None if self.start is None or self.end is None else self.end - self.start

    @property
    def centre(self) -> float | None:
        return None if self.start is None or self.end is None else (self.start + self.end) / 2

    @property
    def cut_frame(self) -> int | None:
        """The cut a centred transition sits on (an odd length puts the extra frame before the cut, as FilmCraft writes it)."""
        return None if self.start is None or self.end is None else self.start + (self.end - self.start + 1) // 2


@dataclass(frozen=True)
class XmlMarker:
    name: str
    comment: str
    frame: int | None


@dataclass
class Fcp7Timeline:
    name: str
    fps: Fraction
    duration: int | None
    width: int | None
    height: int | None
    clips: list[XmlClip] = field(default_factory=list)
    transitions: list[XmlTransition] = field(default_factory=list)
    markers: list[XmlMarker] = field(default_factory=list)
    video_tracks: int = 0
    audio_tracks: int = 0

    def track_clips(self, kind: str, track: int) -> list[XmlClip]:
        return sorted((c for c in self.clips if c.kind == kind and c.track == track), key=lambda c: (c.start if c.start is not None else -1, c.index))

    def counts(self) -> dict[str, int]:
        v = [c for c in self.clips if c.kind == "video"]
        return {"video_clipitems": sum(1 for c in v if not c.is_generator), "title_items": sum(1 for c in v if c.is_generator),
                "audio_clipitems": sum(1 for c in self.clips if c.kind == "audio"), "transitions": len(self.transitions),
                "markers": len(self.markers), "gain_filters": sum(1 for c in self.clips if c.gain_db is not None),
                "speed_items": sum(1 for c in self.clips if c.has_speed)}


def _int(elem: ET.Element | None, tag: str) -> int | None:
    t = (elem.findtext(tag) if elem is not None else None)
    try:
        return None if t is None else int(float(t.strip()))
    except ValueError:
        return None


def _frame(elem: ET.Element, tag: str) -> int | None:
    v = _int(elem, tag)
    return None if v is None or v < 0 else v    # xmeml writes -1 for "not specified / defined by the neighbouring transition"


def parse_rate(elem: ET.Element | None, default: Fraction | None = None) -> Fraction:
    """``<rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>`` -> 30000/1001."""
    rate = elem.find("rate") if elem is not None else None
    if rate is None:
        if default is None:
            raise XmlReadError("element has no <rate>")
        return default
    try:
        tb = int(float((rate.findtext("timebase") or "").strip()))
    except ValueError:
        raise XmlReadError("<rate> has no usable <timebase>") from None
    if tb <= 0:
        raise XmlReadError(f"<timebase> must be positive, got {tb}")
    ntsc = (rate.findtext("ntsc") or "").strip().upper() == "TRUE"
    return Fraction(tb * 1000, 1001) if ntsc else Fraction(tb)


def decode_pathurl(value: str | None) -> str | None:
    """Plain path text of a ``<pathurl>`` (URL, bare Windows path or POSIX path). Case and separators are left as written."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.lower().startswith("file:"):
        return url_to_path(v)
    return v


def _effect_params(filter_elem: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in filter_elem.findall("effect/parameter"):
        pid = (p.findtext("parameterid") or "").strip()
        val = p.find("value")
        if pid and val is not None and val.text is not None and not list(val):
            out[pid] = val.text.strip()
    return out


def _filters(item: ET.Element) -> tuple[str | None, float | None, bool]:
    """(title text, constant gain in dB, has speed/time-remap) from the item's <filter> elements."""
    text: str | None = None
    gain: float | None = None
    speed = item.find("timeremap") is not None
    for f in item.findall("filter"):
        name = (f.findtext("effect/name") or "").strip()
        eid = (f.findtext("effect/effectid") or "").strip().lower()
        params = _effect_params(f)
        if name == "Text" and "text" in params:
            text = params["text"]
        elif name == "Audio Levels" or eid == "audiolevels":
            try:
                lin = float(params.get("level", ""))
                gain = 20 * math.log10(lin) if lin > 0 else float("-inf")
            except ValueError:
                pass
        elif name in ("Time Remap", "Speed") or eid in ("timeremap", "speed"):
            speed = True
    return text, gain, speed


def _resolve_unspecified(tl: "Fcp7Timeline", order: list[tuple[str, int]]) -> None:
    """A clip item next to a transition may say ``<start>-1</start>`` / ``<end>-1</end>``: the transition defines that edge. Fill it with the
    transition's cut (flagged ``*_derived``); without such a neighbour the edge stays unknown (None) and is reported, not guessed."""
    from dataclasses import replace

    for pos, (kind, idx) in enumerate(order):
        if kind != "clip":
            continue
        c = tl.clips[idx]
        changes: dict[str, Any] = {}
        if c.start is None and pos > 0 and order[pos - 1][0] == "transition":
            cut = tl.transitions[order[pos - 1][1]].cut_frame
            if cut is not None:
                changes.update(start=cut, start_derived=True)
        if c.end is None and pos + 1 < len(order) and order[pos + 1][0] == "transition":
            cut = tl.transitions[order[pos + 1][1]].cut_frame
            if cut is not None:
                changes.update(end=cut, end_derived=True)
        if changes:
            tl.clips[idx] = replace(c, **changes)


def parse_fcp7_xml(source: Path | str) -> Fcp7Timeline:
    """Parse a file (``Path``) or XML text (``str`` starting with ``<``/``<?xml``). Raises XmlReadError."""
    try:
        if isinstance(source, Path) or not str(source).lstrip().startswith("<"):
            root = ET.parse(str(source)).getroot()
        else:
            root = ET.fromstring(str(source))
    except (ET.ParseError, OSError) as e:
        raise XmlReadError(f"not readable as XML: {e}") from None
    if root.tag != "xmeml":
        raise XmlReadError(f"not an FCP7 XML (xmeml) document: root element is <{root.tag}>")
    seq = root.find("sequence")
    if seq is None:
        raise XmlReadError("the document has no <sequence>")
    fps = parse_rate(seq)
    fmt = seq.find("media/video/format/samplecharacteristics")
    tl = Fcp7Timeline(name=(seq.findtext("name") or "").strip(), fps=fps, duration=_int(seq, "duration"),
                      width=_int(fmt, "width"), height=_int(fmt, "height"))
    # <file id="x"/> is written in full once and referenced by id afterwards
    paths: dict[str, str | None] = {}
    for f in root.iter("file"):
        fid = f.get("id")
        pu = decode_pathurl(f.findtext("pathurl"))
        if fid and (pu or fid not in paths):
            paths[fid] = pu
    for kind in ("video", "audio"):
        tracks = seq.findall(f"media/{kind}/track")
        setattr(tl, f"{kind}_tracks", len(tracks))
        for ti, track in enumerate(tracks, 1):
            n = 0
            first_clip, first_tr = len(tl.clips), len(tl.transitions)
            order: list[tuple[str, int]] = []      # ("clip"|"transition", index) in file order, for the -1 resolution below
            for item in track:
                if item.tag in ("clipitem", "generatoritem"):
                    n += 1
                    frate = parse_rate(item, fps)
                    fe = item.find("file")
                    path = decode_pathurl(fe.findtext("pathurl")) if fe is not None else None
                    if path is None and fe is not None and fe.get("id"):
                        path = paths.get(fe.get("id", ""))
                    text, gain, speed = _filters(item)
                    generator = item.tag == "generatoritem" or (kind == "video" and path is None)
                    order.append(("clip", len(tl.clips)))
                    tl.clips.append(XmlClip(
                        kind=kind, track=ti, index=n, item_id=item.get("id"), name=(item.findtext("name") or "").strip(),
                        start=_frame(item, "start"), end=_frame(item, "end"), source_in=_int(item, "in"), source_out=_int(item, "out"),
                        rate=frate, path=path, is_generator=generator, text=text, gain_db=gain, has_speed=speed,
                        enabled=(item.findtext("enabled") or "TRUE").strip().upper() != "FALSE"))
                elif item.tag == "transitionitem":
                    order.append(("transition", len(tl.transitions)))
                    tl.transitions.append(XmlTransition(kind, ti, _frame(item, "start"), _frame(item, "end"),
                                                        (item.findtext("alignment") or "").strip() or None,
                                                        (item.findtext("effect/name") or "").strip()))
            _resolve_unspecified(tl, order)
    for m in seq.findall("marker"):
        tl.markers.append(XmlMarker((m.findtext("name") or "").strip(), (m.findtext("comment") or "").strip(), _frame(m, "in")))
    return tl


def timeline_to_dict(tl: Fcp7Timeline) -> dict[str, Any]:
    return {"name": tl.name, "fps": str(tl.fps), "duration": tl.duration, "width": tl.width, "height": tl.height,
            "video_tracks": tl.video_tracks, "audio_tracks": tl.audio_tracks, "counts": tl.counts()}
