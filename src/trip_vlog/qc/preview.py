"""Contact sheets of the rendered preview around each cut (DESIGN §14 steps 4-5) and their machine-readable index.

Layout of the data model (consumed by reviewers and by the QC critic):

  * ``plan_tiles`` (pure) decides WHICH frames are sampled: the first and last frame, and for every cut one frame just before
    it (the end of the outgoing clip) and one just after it (the start of the incoming clip). Every tile knows the record
    frame/seconds, the ``clip_instance_id`` and ``scene_id`` of the clip it shows and its ``side``
    (``start`` | ``before`` | ``after`` | ``end``). A before/after pair is never split across two sheets.
  * ``build_preview_sheets`` grabs the frames with ffmpeg and composes them into one or several JPEG sheets
    (``qc.contact_sheet_max_frames`` tiles per sheet, at most ``qc.contact_sheet_max_sheets`` sheets; with more cuts than
    fit, cuts are sampled evenly and the index says so). Each tile is labelled with its record time, scene and clip.
  * ``PreviewSheetIndex`` is ``qc/preview_sheet.json``: sheet files plus ``tiles`` (tile id -> record_seconds, clip_instance_id,
    scene_id, side, position on its sheet) and ``cuts`` (outgoing/incoming clip and the two tiles of every cut).
"""

from __future__ import annotations

import glob
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.media.tools import popen_flags

INDEX_SCHEMA = 1
SIDES = ("start", "before", "after", "end")


# ---- planning (pure) -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class CutClip:
    """A clip on the primary video track, as far as the sheet is concerned."""
    clip_instance_id: str
    scene_id: str
    start_frame: int
    end_frame: int   # exclusive


@dataclass(frozen=True)
class TileSpec:
    tile_id: str
    record_frame: int
    side: str                      # start | before | after | end
    clip_instance_id: str
    scene_id: str
    cut_index: int | None          # 1-based; None for start/end
    record_seconds: float


@dataclass(frozen=True)
class CutInfo:
    cut_index: int
    record_frame: int
    record_seconds: float
    outgoing_clip_instance_id: str
    incoming_clip_instance_id: str
    outgoing_scene_id: str
    incoming_scene_id: str
    before_tile: str | None = None
    after_tile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"cut_index": self.cut_index, "record_frame": self.record_frame, "record_seconds": self.record_seconds,
                "outgoing_clip_instance_id": self.outgoing_clip_instance_id, "incoming_clip_instance_id": self.incoming_clip_instance_id,
                "outgoing_scene_id": self.outgoing_scene_id, "incoming_scene_id": self.incoming_scene_id,
                "before_tile": self.before_tile, "after_tile": self.after_tile}


@dataclass
class TilePlan:
    groups: list[list[TileSpec]]       # tiles that must stay on one sheet together
    cuts: list[CutInfo]
    cuts_total: int
    cuts_sampled: int

    @property
    def tiles(self) -> list[TileSpec]:
        return [t for g in self.groups for t in g]


def _secs(frame: int, fps: Fraction) -> float:
    return round(float(Fraction(frame) / fps), 4)


def plan_tiles(clips: Sequence[CutClip], fps: Fraction, duration_frames: int, *, offset_seconds: float = 0.15,
               max_per_sheet: int = 48, max_sheets: int = 8, transition_frames: Mapping[str, int] | None = None) -> TilePlan:
    """Which frames to sample. ``clips`` are the primary-video clips in any order; frames are sequence frames.

    The sampled frames stay inside the clip they belong to (an offset longer than a short clip is clamped), so a tile's
    ``clip_instance_id`` is the clip really visible in it. ``transition_frames`` maps an incoming clip_instance_id to the length of the
    dissolve centred on its cut: the offset at that cut is at least half the dissolve plus one frame, so the before/after tiles show
    the clips themselves and not the blend (unless a clip is too short for that; the tile then stays inside its clip)."""
    if not clips or duration_frames <= 0:
        return TilePlan([], [], 0, 0)
    ordered = sorted(clips, key=lambda c: (c.start_frame, c.clip_instance_id))
    off = max(1, round(float(offset_seconds) * float(fps)))
    last_frame = duration_frames - 1
    cuts_all: list[tuple[CutClip, CutClip, int]] = [(a, b, b.start_frame) for a, b in zip(ordered, ordered[1:]) if b.start_frame > 0]
    def pick(keep: int) -> list[tuple[CutClip, CutClip, int]]:
        if len(cuts_all) <= keep:
            return cuts_all
        idx = sorted({round(i * (len(cuts_all) - 1) / (keep - 1)) for i in range(keep)}) if keep > 1 else [0]
        return [cuts_all[i] for i in idx]

    def pairs_fit(keep: int) -> bool:   # start + pairs + end must fit on max_sheets sheets without splitting a pair
        per = max(2, max_per_sheet)
        sheets, used = 1, 1
        for _ in pick(keep):
            if used + 2 > per:
                sheets, used = sheets + 1, 0
            used += 2
        return sheets + (used + 1 > per) <= max(1, max_sheets)

    keep_n = len(cuts_all)
    while keep_n > 1 and not pairs_fit(keep_n):
        keep_n -= 1
    picked = pick(keep_n)
    numbering = {id(c): n for n, c in enumerate(cuts_all, 1)}   # cut_index stays the real position in the cut list
    groups: list[list[TileSpec]] = []
    cuts: list[CutInfo] = []
    n_tile = 0

    def tile(frame: int, side: str, clip: CutClip, cut_index: int | None) -> TileSpec:
        nonlocal n_tile
        n_tile += 1
        frame = min(max(frame, 0), last_frame)
        return TileSpec(f"tile_{n_tile:03d}", frame, side, clip.clip_instance_id, clip.scene_id, cut_index, _secs(frame, fps))

    first, last = ordered[0], ordered[-1]
    groups.append([tile(first.start_frame, "start", first, None)])
    for cut in picked:
        a, b, c = cut
        n = numbering[id(cut)]
        half = -(-int((transition_frames or {}).get(b.clip_instance_id, 0)) // 2)    # a centred dissolve reaches ceil(n/2) frames before the cut
        o = max(off, half + 1) if half else off
        before = tile(min(max(c - o, a.start_frame), c - 1, a.end_frame - 1), "before", a, n)
        after = tile(min(max(c + o, c), min(b.end_frame - 1, last_frame)), "after", b, n)
        groups.append([before, after])
        cuts.append(CutInfo(n, c, _secs(c, fps), a.clip_instance_id, b.clip_instance_id, a.scene_id, b.scene_id,
                            before.tile_id, after.tile_id))
    groups.append([tile(min(last.end_frame - 1, last_frame), "end", last, None)])
    return TilePlan(groups, cuts, len(cuts_all), len(picked))


def split_sheets(groups: Sequence[Sequence[TileSpec]], max_per_sheet: int) -> list[list[TileSpec]]:
    """Fill sheets greedily with whole groups (a before/after pair stays together)."""
    sheets: list[list[TileSpec]] = []
    cur: list[TileSpec] = []
    for g in groups:
        if cur and len(cur) + len(g) > max_per_sheet:
            sheets.append(cur)
            cur = []
        cur.extend(g)
    if cur:
        sheets.append(cur)
    return sheets


def sheet_path_for(first: Path, number: int) -> Path:
    """Sheet 1 keeps the configured name (``contact_sheet_timeline.jpg``); the next ones get ``_02``, ``_03``, ..."""
    return first if number == 1 else first.with_name(f"{first.stem}_{number:02d}{first.suffix}")


# ---- index ---------------------------------------------------------------------------------------------

@dataclass
class TileEntry:
    tile_id: str
    sheet: int
    position: int
    row: int
    col: int
    record_seconds: float
    record_frame: int
    clip_instance_id: str
    scene_id: str
    side: str
    cut_index: int | None
    extracted: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"sheet": self.sheet, "position": self.position, "row": self.row, "col": self.col, "record_seconds": self.record_seconds,
             "record_frame": self.record_frame, "clip_instance_id": self.clip_instance_id, "scene_id": self.scene_id,
             "side": self.side, "cut_index": self.cut_index, "extracted": self.extracted}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class SheetEntry:
    number: int
    path: str
    tiles: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"sheet": self.number, "path": self.path, "tiles": list(self.tiles)}


@dataclass
class PreviewSheetIndex:
    preview: str
    plan_hash: str | None
    fps: str
    duration_frames: int
    columns: int
    tile_width: int
    offset_seconds: float
    sheets: list[SheetEntry] = field(default_factory=list)
    tiles: dict[str, TileEntry] = field(default_factory=dict)
    cuts: list[CutInfo] = field(default_factory=list)
    cuts_total: int = 0
    cuts_sampled: int = 0
    error: str | None = None
    generated_at: str | None = None

    @property
    def truncated(self) -> bool:
        return self.cuts_sampled < self.cuts_total

    def tiles_for_clip(self, clip_instance_id: str) -> list[TileEntry]:
        return [t for t in self.tiles.values() if t.clip_instance_id == clip_instance_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INDEX_SCHEMA, "generated_at": self.generated_at, "preview": self.preview, "plan_hash": self.plan_hash,
            "fps": self.fps, "duration_frames": self.duration_frames, "columns": self.columns, "tile_width": self.tile_width,
            "offset_seconds": self.offset_seconds, "cuts_total": self.cuts_total, "cuts_sampled": self.cuts_sampled,
            "truncated": self.truncated, "error": self.error,
            "sheets": [s.to_dict() for s in self.sheets],
            "tiles": {k: v.to_dict() for k, v in self.tiles.items()},
            "cuts": [c.to_dict() for c in self.cuts],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PreviewSheetIndex":
        if d.get("schema_version") != INDEX_SCHEMA:
            raise ValueError(f"unsupported preview_sheet.json schema_version {d.get('schema_version')!r}")
        idx = cls(d["preview"], d.get("plan_hash"), d["fps"], int(d["duration_frames"]), int(d["columns"]), int(d["tile_width"]),
                  float(d["offset_seconds"]), cuts_total=int(d.get("cuts_total", 0)), cuts_sampled=int(d.get("cuts_sampled", 0)),
                  error=d.get("error"), generated_at=d.get("generated_at"))
        idx.sheets = [SheetEntry(int(s["sheet"]), s["path"], list(s["tiles"])) for s in d.get("sheets", [])]
        for k, v in (d.get("tiles") or {}).items():
            idx.tiles[k] = TileEntry(k, int(v["sheet"]), int(v["position"]), int(v["row"]), int(v["col"]), float(v["record_seconds"]),
                                     int(v["record_frame"]), v["clip_instance_id"], v["scene_id"], v["side"], v.get("cut_index"),
                                     bool(v.get("extracted", True)), v.get("error"))
        idx.cuts = [CutInfo(int(c["cut_index"]), int(c["record_frame"]), float(c["record_seconds"]), c["outgoing_clip_instance_id"],
                            c["incoming_clip_instance_id"], c["outgoing_scene_id"], c["incoming_scene_id"], c.get("before_tile"),
                            c.get("after_tile")) for c in d.get("cuts", [])]
        return idx


def load_preview_sheet_index(path: Path | str) -> PreviewSheetIndex:
    import json

    return PreviewSheetIndex.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---- frame grabs and composition ------------------------------------------------------------------------

def grab_frame(ffmpeg: Path, video: Path, t: float, out: Path, width: int, timeout: float = 60.0) -> str | None:
    cp = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                         "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)],
                        capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                        shell=False, **popen_flags())
    if cp.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        return (cp.stderr or "no frame produced").strip()[-200:]
    return None


def _ts(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:06.3f}"


def tile_label(t: TileSpec) -> tuple[str, str]:
    """Two label lines: what it is and when, then which scene and clip."""
    head = {"start": "start", "end": "end"}.get(t.side) or f"cut {t.cut_index} {t.side}"
    return f"{head}   {_ts(t.record_seconds)}", f"{t.scene_id}  {t.clip_instance_id}"


def _fit(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)] + "…"


@dataclass
class SheetsResult:
    index: PreviewSheetIndex
    written: list[Path]
    failed: list[str]

    @property
    def frames(self) -> int:
        return sum(1 for t in self.index.tiles.values() if t.extracted)


def build_preview_sheets(ffmpeg: Path, preview: Path, first_sheet: Path, plan: TilePlan, fps: Fraction, *, columns: int = 4,
                         tile_width: int = 320, max_per_sheet: int = 48, offset_seconds: float = 0.15, duration_frames: int = 0,
                         plan_hash: str | None = None, preview_label: str | None = None,
                         rel: Any = None) -> SheetsResult:
    """Grab every planned frame and write the sheets (``first_sheet``, ``..._02`` ...). ``rel`` maps a Path to the text stored in
    the index (workspace-relative); frames that cannot be grabbed leave a gap tile and are listed in ``failed``."""
    from PIL import Image, ImageDraw, ImageFont

    rel = rel or (lambda p: Path(p).as_posix())
    idx = PreviewSheetIndex(preview_label or rel(preview), plan_hash, str(fps), duration_frames, columns, tile_width, offset_seconds,
                            cuts=list(plan.cuts), cuts_total=plan.cuts_total, cuts_sampled=plan.cuts_sampled)
    sheets = split_sheets(plan.groups, max_per_sheet)
    failed: list[str] = []
    images: dict[str, "Image.Image"] = {}
    with tempfile.TemporaryDirectory(prefix="tv-sheet-") as td:
        for i, t in enumerate(plan.tiles):
            f = Path(td) / f"f{i:04d}.jpg"
            try:
                err = grab_frame(ffmpeg, preview, max(0.0, float((Fraction(t.record_frame) - Fraction(1, 4)) / fps)), f, tile_width)
            except (subprocess.TimeoutExpired, OSError) as ex:  # treat like any other failed frame; real bugs stay loud
                err = f"{type(ex).__name__}: {ex}"
            if err:
                failed.append(f"{t.tile_id} ({t.side} @ {_ts(t.record_seconds)}, {t.clip_instance_id}): {err}")
                images[t.tile_id] = None  # type: ignore[assignment]
                continue
            with Image.open(f) as im:
                images[t.tile_id] = im.convert("RGB").copy()
    if not any(im is not None for im in images.values()):
        idx.error = "no frames could be extracted from the preview"
        remove_stale_sheets(first_sheet, None)      # sheets of an earlier run would describe a different preview
        for sn, tiles in enumerate(sheets, 1):  # still record what was planned
            idx.sheets.append(SheetEntry(sn, "", [t.tile_id for t in tiles]))
        for sn, tiles in enumerate(sheets, 1):
            for pos, t in enumerate(tiles):
                idx.tiles[t.tile_id] = TileEntry(t.tile_id, sn, pos, pos // columns, pos % columns, t.record_seconds, t.record_frame,
                                                 t.clip_instance_id, t.scene_id, t.side, t.cut_index, False, "frame not extracted")
        return SheetsResult(idx, [], failed)
    th = max(im.height for im in images.values() if im is not None)
    label_h, pad = 32, 6
    chars = max(8, int(tile_width / 6.6))
    try:
        font = ImageFont.load_default(size=12)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()
    written: list[Path] = []
    keep: set[Path] = set()
    for sn, tiles in enumerate(sheets, 1):
        cols = min(columns, len(tiles))
        rows = -(-len(tiles) // columns)
        sheet = Image.new("RGB", (cols * (tile_width + pad) + pad, rows * (th + label_h + pad) + pad), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for pos, t in enumerate(tiles):
            r, c = divmod(pos, columns)
            x, y = pad + c * (tile_width + pad), pad + r * (th + label_h + pad)
            im = images[t.tile_id]
            l1, l2 = tile_label(t)
            draw.text((x + 2, y + 1), _fit(l1, chars), fill=(235, 235, 235), font=font)
            draw.text((x + 2, y + 16), _fit(l2, chars), fill=(170, 200, 235), font=font)
            if im is not None:
                sheet.paste(im, (x, y + label_h))
            else:
                draw.rectangle((x, y + label_h, x + tile_width - 1, y + label_h + th - 1), outline=(120, 60, 60))
            idx.tiles[t.tile_id] = TileEntry(t.tile_id, sn, pos, r, c, t.record_seconds, t.record_frame, t.clip_instance_id, t.scene_id,
                                             t.side, t.cut_index, im is not None, None if im is not None else "frame not extracted")
        out = sheet_path_for(first_sheet, sn)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.tmp.jpg")
        sheet.save(tmp, "JPEG", quality=85)
        tmp.replace(out)
        written.append(out)
        keep.add(out)
        idx.sheets.append(SheetEntry(sn, rel(out), [t.tile_id for t in tiles]))
    for stale in _numbered_sheets(first_sheet):          # leftovers of an earlier, longer run
        if stale not in keep:
            stale.unlink(missing_ok=True)
    return SheetsResult(idx, written, failed)


def _numbered_sheets(first_sheet: Path) -> list[Path]:
    """``<stem>_02<suffix>``, ``_03``, ... next to the first sheet. The stem is glob-escaped: file names may contain ``[`` or ``*``."""
    return sorted(first_sheet.parent.glob(f"{glob.escape(first_sheet.stem)}_[0-9][0-9]{glob.escape(first_sheet.suffix)}"))


def remove_stale_sheets(first_sheet: Path, index_path: Path | None) -> None:
    """A QC run that makes no preview sheets must not leave the sheets and ``preview_sheet.json`` of an earlier run behind: they would
    describe a different preview."""
    for p in [first_sheet, *_numbered_sheets(first_sheet), *([index_path] if index_path is not None else [])]:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
