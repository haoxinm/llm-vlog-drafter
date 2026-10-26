"""Hand-written FCP7 XML (xmeml) builders for the parser / round-trip / compatibility tests: Windows and macOS style paths, NTSC rates."""
from __future__ import annotations

import re
import urllib.parse
from typing import Any
from xml.sax.saxutils import escape


def to_url(path: str, style: str = "localhost") -> str:
    """file URL the way FCP7 exporters write it: ``file://localhost/C%3a/Users/Me/a%20b.mp4`` (Windows) or ``file://localhost/Users/me/a.mov``."""
    p = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        drive, rest = p[0], p[2:]
        q = f"/{drive}%3a" + urllib.parse.quote(rest, safe="/")
        return ("file://localhost" if style == "localhost" else "file://") + (q if style == "localhost" else q.replace("%3a", ":"))
    return ("file://localhost" if style == "localhost" else "file://") + urllib.parse.quote(p, safe="/")


def _rate(tb: int, ntsc: bool) -> str:
    return f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"


def clipitem(i: int, name: str, start: int, end: int, in_: int, out: int, *, path: str | None = None, file_id: str | None = None, tb: int = 30,
             ntsc: bool = False, text: str | None = None, gain: float | None = None, speed: bool = False, url_style: str = "localhost",
             full_file: bool = True) -> str:
    f = ""
    if file_id and path and full_file:
        f = f'<file id="{file_id}"><name>{escape(name)}</name><pathurl>{escape(to_url(path, url_style))}</pathurl></file>'
    elif file_id and path:
        f = f'<file id="{file_id}"/>'
    elif text is not None:
        f = '<file id="title-file"><name>Text</name></file>'
    flt = ""
    if text is not None:
        flt += ("<filter><effect><name>Text</name><effectid>Text</effectid><parameter><parameterid>text</parameterid><name>Text</name>"
                f"<value>{escape(text)}</value></parameter></effect></filter>")
    if gain is not None:
        lin = 10 ** (gain / 20)
        flt += ("<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><parameter><parameterid>level</parameterid>"
                f"<value>{lin}</value></parameter></effect></filter>")
    if speed:
        flt += "<filter><effect><name>Time Remap</name><effectid>timeremap</effectid></effect></filter>"
    return (f'<clipitem id="clipitem-{i}"><name>{escape(name)}</name><enabled>TRUE</enabled><duration>{out - in_}</duration>{_rate(tb, ntsc)}'
            f"<start>{start}</start><end>{end}</end><in>{in_}</in><out>{out}</out>{f}{flt}</clipitem>")


def transitionitem(start: int, end: int, *, tb: int = 30, ntsc: bool = False, name: str = "Cross Dissolve") -> str:
    return (f"<transitionitem>{_rate(tb, ntsc)}<start>{start}</start><end>{end}</end><alignment>center</alignment>"
            f"<effect><name>{name}</name><effectid>{name}</effectid></effect></transitionitem>")


def xmeml(*, duration: int, tb: int = 30, ntsc: bool = False, width: int = 1920, height: int = 1080, video: list[list[str]] = (),
          audio: list[list[str]] = (), markers: list[tuple[str, str, int]] = (), name: str = "Main Draft") -> str:
    vt = "".join(f"<track>{''.join(t)}<enabled>TRUE</enabled><locked>FALSE</locked></track>" for t in video)
    at = "".join(f"<track>{''.join(t)}<enabled>TRUE</enabled><locked>FALSE</locked></track>" for t in audio)
    mk = "".join(f"<marker><comment>{escape(c)}</comment><name>{escape(n)}</name><in>{f}</in><out>-1</out></marker>" for n, c, f in markers)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4"><sequence id="sequence-1">'
            f"<name>{escape(name)}</name><duration>{duration}</duration>{_rate(tb, ntsc)}<media><video><format><samplecharacteristics>{_rate(tb, ntsc)}"
            f"<width>{width}</width><height>{height}</height></samplecharacteristics></format>{vt}</video><audio>{at}</audio></media>{mk}</sequence></xmeml>")


def xml_for_spec(spec: Any, *, ntsc: bool | None = None, url_style: str = "localhost", mutate: Any = None) -> str:
    """The FCP7 XML a perfect exporter would write for ``spec`` (a TimelineSpec); ``mutate(items)`` may alter the item dicts first."""
    from fractions import Fraction

    from trip_vlog.handoff.xml_roundtrip import expected_from_spec

    exp = expected_from_spec(spec)
    fps = Fraction(spec.fps)
    is_ntsc = fps.denominator == 1001 if ntsc is None else ntsc
    tb = round(float(fps))
    items: list[dict[str, Any]] = []
    for n, e in enumerate(exp.clips, 1):
        items.append({"i": n, "kind": e.kind, "track": e.track, "name": e.text or (e.path or "x").replace("\\", "/").rsplit("/", 1)[-1], "start": e.start, "end": e.end,
                      "in_": e.source_in if e.source_in is not None else 0, "out": e.source_out if e.source_out is not None else e.end - e.start,
                      "path": e.path, "text": e.text, "gain": e.gain_db if (e.kind == "audio" and e.gain_db) else None})
    trans = [{"track": t.track, "start": t.cut_frame - t.frames // 2, "end": t.cut_frame - t.frames // 2 + t.frames} for t in exp.transitions]
    marks = [(m.name, m.comment, m.frame) for m in exp.markers]
    doc = {"items": items, "transitions": trans, "markers": marks, "duration": exp.duration_frames, "width": exp.width, "height": exp.height}
    if mutate:
        mutate(doc)
    ntracks = {"video": max([it["track"] for it in doc["items"] if it["kind"] == "video"] + [1]),
               "audio": max([it["track"] for it in doc["items"] if it["kind"] == "audio"] + [1])}
    seen: dict[str, str] = {}
    tracks: dict[str, list[list[str]]] = {"video": [[] for _ in range(ntracks["video"])], "audio": [[] for _ in range(ntracks["audio"])]}
    for it in sorted(doc["items"], key=lambda x: x["start"]):
        fid = seen.setdefault(it["path"], f"file-{len(seen) + 1}") if it["path"] else None
        tracks[it["kind"]][it["track"] - 1].append(clipitem(it["i"], it["name"], it["start"], it["end"], it["in_"], it["out"], path=it["path"], file_id=fid,
                                                            tb=tb, ntsc=is_ntsc, text=it["text"], gain=it["gain"], speed=it.get("speed", False),
                                                            url_style=url_style, full_file=fid is not None and it["path"] not in {x["path"] for x in doc["items"] if x["i"] < it["i"]}))
    for t in doc["transitions"]:
        tracks["video"][t["track"] - 1].append(transitionitem(t["start"], t["end"], tb=tb, ntsc=is_ntsc))
    return xmeml(duration=doc["duration"], tb=tb, ntsc=is_ntsc, width=doc["width"], height=doc["height"], video=tracks["video"], audio=tracks["audio"],
                 markers=doc["markers"])
