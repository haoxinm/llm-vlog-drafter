"""Identity mappings kept by the adapter: asset path -> FilmCraft media item id, clip_instance_id -> timeline clip ids."""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


def canon_path(p: str | os.PathLike[str]) -> str:
    """Comparable form of a filesystem path or ``file://`` URL.

    Forward slashes, normalised (``..`` collapsed), case-folded on Windows (``normcase``), ``\\\\?\\`` prefix and
    ``file://localhost`` / ``file:///C:/`` URL forms decoded. Used only for comparing, never for opening files.
    """
    s = os.fspath(p)
    if s.lower().startswith("file:"):
        u = re.sub(r"^file://localhost", "file://", s, flags=re.I)
        s = urllib.parse.unquote(urllib.parse.urlparse(u).path)
        if re.match(r"^/[A-Za-z]:[/\\]", s):
            s = s[1:]
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.normpath(s)).replace("\\", "/")


@dataclass
class MediaMap:
    """asset ``source_path`` (as given in the spec) <-> FilmCraft media item id."""

    item_by_path: dict[str, int] = field(default_factory=dict)  # canonical path -> item id
    path_by_item: dict[int, str] = field(default_factory=dict)  # item id -> original path string

    def add(self, path: str, item: int) -> None:
        self.item_by_path[canon_path(path)] = item
        self.path_by_item[item] = path

    def item_for(self, path: str) -> int:
        try:
            return self.item_by_path[canon_path(path)]
        except KeyError:
            raise KeyError(f"asset {path!r} was not imported into the FilmCraft project") from None

    def get(self, path: str) -> int | None:
        return self.item_by_path.get(canon_path(path))

    def to_dict(self) -> dict[str, int]:
        return {self.path_by_item[i]: i for i in sorted(self.path_by_item)}

    @classmethod
    def from_dict(cls, d: Mapping[str, int]) -> "MediaMap":
        m = cls()
        for path, item in d.items():
            m.add(path, int(item))
        return m


@dataclass(frozen=True)
class ClipRef:
    """FilmCraft timeline clip ids created for one ``clip_instance_id``."""

    clip_instance_id: str
    track: str
    clip: int  # primary clip (video/image clip, or the audio clip of an audio-only placement)
    audio_clip: int | None = None  # linked audio clip of a video placement
    audio_track: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"track": self.track, "clip": self.clip, "audio_clip": self.audio_clip, "audio_track": self.audio_track}


@dataclass
class ClipMap:
    refs: dict[str, ClipRef] = field(default_factory=dict)
    titles: list[int] = field(default_factory=list)  # title clip ids, in spec order
    markers: list[int] = field(default_factory=list)
    transitions: dict[str, int] = field(default_factory=dict)  # clip_instance_id -> transition id

    def __getitem__(self, clip_instance_id: str) -> ClipRef:
        return self.refs[clip_instance_id]

    def __contains__(self, clip_instance_id: object) -> bool:
        return clip_instance_id in self.refs

    def __len__(self) -> int:
        return len(self.refs)

    def to_dict(self) -> dict[str, Any]:
        return {"clips": {k: v.to_dict() for k, v in self.refs.items()}, "titles": self.titles,
                "markers": self.markers, "transitions": self.transitions}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ClipMap":
        cm = cls()
        for k, v in (d.get("clips") or {}).items():
            cm.refs[k] = ClipRef(k, v["track"], v["clip"], v.get("audio_clip"), v.get("audio_track"))
        cm.titles = list(d.get("titles", []))
        cm.markers = list(d.get("markers", []))
        cm.transitions = dict(d.get("transitions", {}))
        return cm


def build_media_map(paths: Iterable[str], status_rows: Iterable[Mapping[str, Any]]) -> MediaMap:
    """Map each requested path to the item FilmCraft reports for it (``media.status`` rows: item + path).

    ``media.status`` is authoritative: it says which item points at which original file, so the mapping stays
    correct even if an import partially failed or items were numbered unexpectedly.
    """
    by_canon = {canon_path(r["path"]): int(r["item"]) for r in status_rows if r.get("path") is not None}
    mm = MediaMap()
    missing = []
    for p in paths:
        item = by_canon.get(canon_path(p))
        if item is None:
            missing.append(p)
        else:
            mm.add(p, item)
    if missing:
        raise KeyError("FilmCraft did not report these imported assets: " + ", ".join(missing))
    return mm
