"""Interchange export post-processing and the HandoffReport (DESIGN §15).

FilmCraft's own ``file.exportInterchange`` is the exporter. Phase 0 found that OTIO ``target_url`` and EDL
``SOURCE FILE`` entries are written *relative to the exported file* when media and export share a top-level
directory (gap 2). Resolve (and any tool run from another directory) needs absolute references, so every
export is post-processed to absolute ``file://`` URLs / paths, reloaded, and re-validated against the
timeline read back from FilmCraft.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from trip_vlog.filmcraft.inspect import SequenceSnapshot
from trip_vlog.filmcraft.mappings import canon_path
from trip_vlog.filmcraft.preview import Check
from trip_vlog.filmcraft.timeline_spec import TimelineSpec
from trip_vlog.state import atomic_write_text


@dataclass(frozen=True)
class FormatInfo:
    key: str  # name used in config / API
    fc_format: str  # file.exportInterchange "format" value
    filename: str
    role: str
    loses: tuple[str, ...]


# "loses" comes from the executed Phase 0 spike (docs/phase0-filmcraft-spike.md section 6) - not from Resolve.
FORMATS: dict[str, FormatInfo] = {
    "otio": FormatInfo("otio", "otio", "timeline.otio", "primary", (
        "Titles are not carried: written as a clip with a missing media reference (text/style only in metadata.filmcraft).",
        "An audio fade-out is exported as an SMPTE_Dissolve transition at the clip end.",
        "Effects/keyframes other than the cut points, dissolves, markers and clip gain are not represented.",
        "FilmCraft writes target_url relative to the .otio file; the adapter rewrites them to absolute file:// URLs.",
    )),
    "fcp7_xml": FormatInfo("fcp7_xml", "xml", "timeline.xml", "fallback", (
        "Titles are not carried: written as a clipitem without media (Source Text filter parameters only).",
        "Clip gain is folded into an Audio Levels filter.",
        "Media URLs are absolute file://localhost/... (no rewriting needed).",
    )),
    "fcpxml": FormatInfo("fcpxml", "fcpxml", "timeline.fcpxml", "last resort", (
        "Titles become an asset-clip with a dangling ref (importers may reject it).",
        "Transitions on tracks other than V1 are dropped (audio fades lost).",
        "A still gets a 29.97 <format> inside a 30p sequence.",
    )),
    "edl": FormatInfo("edl", "edl", "timeline.edl", "last resort", (
        "Only one video track (V1) is exported: V2 clips and titles are lost.",
        "No frame rate is stored in the file: importers must be told the sequence rate.",
        "SOURCE FILE comments are relative to the .edl (the adapter rewrites them to absolute paths).",
        "Centre-aligned dissolves clamp the source-in when handles are missing.",
    )),
    "aaf": FormatInfo("aaf", "aaf", "timeline.aaf", "last resort", (
        "Titles are left as gaps.", "Only the container structure could be checked (no AAF library).",
    )),
    "omf": FormatInfo("omf", "omf", "timeline.omf", "audio only", (
        "Audio only; markers are not written.",
    )),
}
FORMAT_ALIASES = {"xml": "fcp7_xml", "fcp7": "fcp7_xml", "fcpxml": "fcpxml", "otio": "otio", "edl": "edl", "aaf": "aaf", "omf": "omf",
                  "fcp7_xml": "fcp7_xml"}
GLOBAL_LIMITS = (
    "No interchange format carries editable titles; recreate them in Resolve from the title list below (or use the preview MP4).",
    "Resolve import has not been verified by this tool; run `trip-vlog verify-resolve` on a machine with Resolve to confirm fidelity.",
    "Media is referenced by absolute path: keep the original media at the same location (or relink).",
)


def resolve_format(name: str) -> FormatInfo:
    key = FORMAT_ALIASES.get(name.lower())
    if key is None:
        raise ValueError(f"unknown interchange format {name!r}; use one of {sorted(FORMATS)}")
    return FORMATS[key]


@dataclass
class FormatReport:
    format: str
    fc_format: str
    path: str
    role: str
    exported: bool = False
    ok: bool = False
    bytes: int = 0
    engine_report: list[str] = field(default_factory=list)
    postprocess: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    loses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "checks"}
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


@dataclass
class HandoffReport:
    out_dir: str
    formats: list[FormatReport] = field(default_factory=list)
    titles_not_carried: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    filmcraft_version: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.formats) and all(f.ok for f in self.formats)

    @property
    def files(self) -> list[str]:
        return [f.path for f in self.formats if f.exported]

    def primary(self) -> FormatReport | None:
        return next((f for f in self.formats if f.role == "primary" and f.ok), None) or next((f for f in self.formats if f.ok), None)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "out_dir": self.out_dir, "files": self.files, "filmcraft_version": self.filmcraft_version,
                "formats": [f.to_dict() for f in self.formats], "titles_not_carried": self.titles_not_carried,
                "limitations": self.limitations}


# ----------------------------------------------------------------------------
# path / URL helpers
# ----------------------------------------------------------------------------

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def url_to_path(url: str) -> str:
    """Filesystem path text of a ``file://`` URL (handles ``localhost`` and Windows ``/C:/`` forms)."""
    u = re.sub(r"^file://localhost", "file://", url, flags=re.I)
    parsed = urllib.parse.urlparse(u)
    p = urllib.parse.unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return "//" + parsed.netloc + p  # UNC
    if re.match(r"^/[A-Za-z]:[/\\]", p):
        p = p[1:]
    return p


def is_absolute_ref(ref: str) -> bool:
    return ref.startswith("/") or bool(_WIN_ABS.match(ref)) or ref.startswith("\\\\")


def resolve_reference(ref: str, base_dir: Path, known: Mapping[str, str]) -> tuple[Path | None, str | None]:
    """Resolve a media reference (URL or path, absolute or relative to ``base_dir``) to an absolute path.

    ``known`` maps ``canon_path`` -> original absolute path of every imported asset. Returns (path, warning).
    """
    if _SCHEME.match(ref) and not ref.lower().startswith("file:"):
        return None, f"non-file media reference left unchanged: {ref[:80]}"
    if ref.lower().startswith("file:"):
        cand = Path(url_to_path(ref))
    elif is_absolute_ref(urllib.parse.unquote(ref)):
        cand = Path(urllib.parse.unquote(ref))
    else:
        cand = Path(os.path.normpath(base_dir / urllib.parse.unquote(ref)))
    hit = known.get(canon_path(cand))
    if hit is not None:
        return Path(hit), None
    if cand.is_absolute() and cand.exists():
        return cand, None
    base = os.path.basename(cand.as_posix())
    same_name = [v for v in known.values() if os.path.basename(Path(v).as_posix()) == base]
    if len(same_name) == 1:
        return Path(same_name[0]), f"reference {ref!r} did not resolve; matched by file name to {same_name[0]}"
    return None, f"media reference {ref!r} could not be resolved against {base_dir}"


def _walk_dicts(o: Any) -> Iterator[dict[str, Any]]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_dicts(v)


# ----------------------------------------------------------------------------
# OTIO
# ----------------------------------------------------------------------------


def rewrite_otio_urls(path: Path, known: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Make every ``target_url`` an absolute ``file://`` URL. Returns (actions, warnings); rewrites the file in place."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    actions, warnings, changed = [], [], 0
    for d in _walk_dicts(doc):
        url = d.get("target_url")
        if not isinstance(url, str) or not url:
            continue
        target, warn = resolve_reference(url, path.parent, known)
        if warn:
            warnings.append(warn)
        if target is None:
            continue
        new = target.as_uri()
        if new != url:
            d["target_url"] = new
            changed += 1
    if changed:
        atomic_write_text(path, json.dumps(doc, indent=4, ensure_ascii=False) + "\n")
        actions.append(f"rewrote {changed} target_url value(s) to absolute file:// URLs")
    return actions, sorted(set(warnings))


def validate_otio(path: Path, snap: SequenceSnapshot, known: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    add = lambda n, ok, d="": checks.append(Check(n, bool(ok), d))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [Check("otio parses as JSON", False, str(e))]
    add("otio parses as JSON", str(doc.get("OTIO_SCHEMA", "")).startswith("Timeline"), str(doc.get("OTIO_SCHEMA")))
    tracks = {t.get("name"): t for t in doc.get("tracks", {}).get("children", [])}
    bad_urls: list[str] = []
    n_refs = 0
    for d in _walk_dicts(doc):
        url = d.get("target_url")
        if isinstance(url, str) and url:
            n_refs += 1
            p = url_to_path(url) if url.lower().startswith("file:") else None
            if p is None or not is_absolute_ref(p) or not Path(p).exists():
                bad_urls.append(url)
    add("all media references are absolute file URLs of existing files", not bad_urls and n_refs > 0,
        f"{n_refs} reference(s); bad: {bad_urls[:3]}")
    unknown = [u for d in _walk_dicts(doc) for u in [d.get("target_url")] if isinstance(u, str) and u.lower().startswith("file:")
               and canon_path(u) not in known]
    add("media references point at imported assets", not unknown, f"unknown: {unknown[:3]}")
    mismatches: list[str] = []
    for t in snap.tracks:
        otio_track = tracks.get(t.engine_name)
        children = (otio_track or {}).get("children", [])
        n_clips = sum(1 for c in children if str(c.get("OTIO_SCHEMA", "")).startswith("Clip"))
        if n_clips != len(t.clips):
            mismatches.append(f"{t.name}: {len(t.clips)} clips in FilmCraft, {n_clips} in OTIO")
            continue
        pos, i = 0, 0
        for ch in children:
            sr = ch.get("source_range")
            schema = str(ch.get("OTIO_SCHEMA", "")).split(".")[0]
            if schema not in ("Gap", "Clip") or not sr:
                continue
            dur = round(sr["duration"]["value"])
            if schema == "Clip":
                exp = t.clips[i]
                i += 1
                if abs(pos - exp.start_frame) > 1 or abs(dur - exp.duration_frames) > 1:
                    mismatches.append(f"{t.name}[{i - 1}] {exp.name}: OTIO {pos}+{dur}, FilmCraft {exp.start_frame}+{exp.duration_frames}")
            pos += dur
    add("clip counts, starts and durations match the FilmCraft timeline", not mismatches, "; ".join(mismatches[:4]))
    try:
        import opentimelineio as otio  # type: ignore
    except ImportError:
        add("opentimelineio library load", True, "skipped: opentimelineio not installed (stdlib checks only)")
    else:
        try:
            tl = otio.adapters.read_from_file(str(path))
            frames = tl.duration().to_frames()
            add("opentimelineio library load", abs(frames - snap.duration_frames) <= 1,
                f"loaded; {frames} frames at {tl.duration().rate} fps (expected {snap.duration_frames})")
        except Exception as e:  # noqa: BLE001 - any parse failure is a finding
            add("opentimelineio library load", False, str(e)[:300])
    return checks


# ----------------------------------------------------------------------------
# FCP7 XML
# ----------------------------------------------------------------------------


def rewrite_xml_pathurls(path: Path, known: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Rewrite relative ``<pathurl>`` values (FilmCraft writes absolute ones; this is a safety net)."""
    text = path.read_text(encoding="utf-8")
    warnings: list[str] = []
    changed = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal changed
        ref = m.group(2)
        if _SCHEME.match(ref) and ref.lower().startswith("file:") and is_absolute_ref(url_to_path(ref)):
            return m.group(0)
        target, warn = resolve_reference(ref, path.parent, known)
        if warn:
            warnings.append(warn)
        if target is None:
            return m.group(0)
        changed += 1
        return f"{m.group(1)}{target.as_uri()}{m.group(3)}"

    new = re.sub(r"(<pathurl>)([^<]*)(</pathurl>)", repl, text)
    actions = []
    if changed:
        atomic_write_text(path, new)
        actions.append(f"rewrote {changed} pathurl value(s) to absolute file:// URLs")
    return actions, sorted(set(warnings))


def validate_fcp7_xml(path: Path, snap: SequenceSnapshot, known: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    add = lambda n, ok, d="": checks.append(Check(n, bool(ok), d))
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        return [Check("xml is well-formed", False, str(e))]
    add("xml is well-formed", root.tag == "xmeml", root.tag)
    seq = root.find("sequence")
    if seq is None:
        return checks + [Check("has a <sequence>", False)]
    try:
        dur = int(seq.findtext("duration") or -1)
    except ValueError:
        dur = -1
    add("sequence duration matches", abs(dur - snap.duration_frames) <= 1, f"xml {dur}, FilmCraft {snap.duration_frames}")
    bad = []
    n = 0
    for pu in root.iter("pathurl"):
        n += 1
        v = (pu.text or "").strip()
        p = url_to_path(v) if v.lower().startswith("file:") else v
        if not is_absolute_ref(p) or not Path(p).exists() or canon_path(v) not in known:
            bad.append(v)
    add("all pathurl values are absolute URLs of imported assets", not bad and n > 0, f"{n} reference(s); bad: {bad[:3]}")
    counts = []
    for kind, snap_kind in (("video", "video"), ("audio", "audio")):
        xml_tracks = seq.findall(f"media/{kind}/track")
        snap_tracks = [t for t in snap.tracks if t.kind == snap_kind]
        for xt, st in zip(xml_tracks, snap_tracks):
            got = len(xt.findall("clipitem"))
            if got != len(st.clips):
                counts.append(f"{st.name}: xml {got}, FilmCraft {len(st.clips)}")
    add("clipitem counts per track match", not counts, "; ".join(counts))
    return checks


# ----------------------------------------------------------------------------
# EDL
# ----------------------------------------------------------------------------


def rewrite_edl_sources(path: Path, known: Mapping[str, str]) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    warnings: list[str] = []
    changed = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal changed
        ref = m.group(2).strip()
        target, warn = resolve_reference(ref, path.parent, known)
        if warn:
            warnings.append(warn)
        if target is None or str(target) == ref:
            return m.group(0)
        changed += 1
        return f"{m.group(1)}{target}"

    new = re.sub(r"(\* SOURCE FILE:\s*)(.*)", repl, text)
    actions = []
    if changed:
        atomic_write_text(path, new)
        actions.append(f"rewrote {changed} SOURCE FILE entr(ies) to absolute paths")
    return actions, sorted(set(warnings))


def validate_edl(path: Path, snap: SequenceSnapshot, known: Mapping[str, str]) -> list[Check]:
    text = path.read_text(encoding="utf-8")
    events = re.findall(r"^\d{3}\s+\S+\s+\S+\s+\S+", text, re.M)
    srcs = [s.strip() for s in re.findall(r"\* SOURCE FILE:\s*(.*)", text)]
    bad = [s for s in srcs if not is_absolute_ref(s) or not Path(s).exists()]
    return [Check("edl has events", bool(events), f"{len(events)} event(s)"),
            Check("SOURCE FILE entries are absolute and exist", not bad, f"bad: {bad[:3]}")]


def postprocess_and_validate(fmt: FormatInfo, path: Path, snap: SequenceSnapshot, known: Mapping[str, str],
                             rep: FormatReport) -> None:
    """Rewrite relative media references to absolute, then reload and validate; fills ``rep``."""
    if fmt.key == "otio":
        actions, warns = rewrite_otio_urls(path, known)
        checks = validate_otio(path, snap, known)
    elif fmt.key == "fcp7_xml":
        actions, warns = rewrite_xml_pathurls(path, known)
        checks = validate_fcp7_xml(path, snap, known)
    elif fmt.key == "edl":
        actions, warns = rewrite_edl_sources(path, known)
        checks = validate_edl(path, snap, known)
    else:
        actions, warns = [], []
        checks = [Check("file is non-empty", path.stat().st_size > 0)]
    rep.postprocess += actions
    rep.warnings += warns
    rep.checks += checks
    rep.bytes = path.stat().st_size
    rep.ok = all(c.ok for c in checks)


def titles_not_carried(spec: TimelineSpec | None) -> list[dict[str, Any]]:
    if spec is None:
        return []
    return [{"text": t.text, "start_frame": t.start_frame, "duration_frames": t.duration_frames, "track": t.track}
            for t in spec.titles]
