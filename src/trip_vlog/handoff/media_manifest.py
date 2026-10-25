"""media_manifest.csv (DESIGN §15.1): every catalogued asset with absolute path, versions and technical metadata."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable, Sequence

from trip_vlog.state import atomic_write_text

COLUMNS = ("asset_id", "absolute_path", "relative_path", "media_type", "content_version", "sha256", "duration_seconds",
           "width", "height", "fps", "used_in_timeline", "exists_on_disk", "probe_status")


def _fps(row: Any) -> str:
    n, d = row["frame_rate_num"], row["frame_rate_den"]
    if not n or not d:
        return ""
    return str(n) if d == 1 else f"{n}/{d}"


def manifest_rows(assets: Iterable[Any], media_root: Path | str, used_asset_ids: Iterable[str]) -> list[dict[str, Any]]:
    used = set(used_asset_ids)
    out = []
    for r in assets:
        p = Path(media_root).joinpath(*r["relative_path"].split("/"))
        out.append({
            "asset_id": r["asset_id"], "absolute_path": str(p), "relative_path": r["relative_path"],
            "media_type": r["media_type"] or "", "content_version": r["content_version"] or "", "sha256": r["sha256"] or "",
            "duration_seconds": "" if r["duration_seconds"] is None else f"{r['duration_seconds']:.6f}".rstrip("0").rstrip("."),
            "width": r["width"] or "", "height": r["height"] or "", "fps": _fps(r),
            "used_in_timeline": "yes" if r["asset_id"] in used else "no",
            "exists_on_disk": "yes" if p.is_file() else "no", "probe_status": r["probe_status"]})
    return sorted(out, key=lambda x: x["relative_path"])


def write_media_manifest(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    atomic_write_text(path, buf.getvalue())


def missing_used_media(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [r["absolute_path"] for r in rows if r["used_in_timeline"] == "yes" and r["exists_on_disk"] != "yes"]
