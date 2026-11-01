"""Interchange export for the handoff package (DESIGN §15.1, §15.3): FilmCraft's exporter, never hand-built files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from trip_vlog.filmcraft.errors import FilmCraftError
from trip_vlog.filmcraft.interchange import HandoffReport, resolve_format
from trip_vlog.filmcraft.timeline_spec import TimelineSpec

# Observed on Resolve 21.1 with the Phase 0 fixture (user-run import of the spike exports).
RESOLVE_OBSERVATIONS: dict[str, str] = {
    "fcp7_xml": "verified to import in Resolve 21.1 (Phase 0 fixture)",
    "otio": "imports in Resolve 21.1; media linking with absolute paths not yet verified "
            "(the Phase 0 export, which used relative target_url paths, reported 5 media files not found)",
    "aaf": "Resolve 21.1 reported that the file cannot be opened for reading",
    "fcpxml": "crashed Resolve 21.1",
    "edl": "froze Resolve 21.1",
}
DANGEROUS_FORMATS = ("fcpxml", "edl", "aaf")


@dataclass
class HandoffExport:
    requested: list[str]
    report: HandoffReport | None = None
    error: str | None = None
    otio_reload: dict[str, Any] | None = None
    dangerous_requested: list[str] = field(default_factory=list)

    @property
    def exported_files(self) -> list[str]:
        return self.report.files if self.report else []

    def to_dict(self) -> dict[str, Any]:
        return {"requested": self.requested, "error": self.error, "otio_reload": self.otio_reload,
                "dangerous_requested": self.dangerous_requested, "report": self.report.to_dict() if self.report else None}


def normalize_formats(formats: Sequence[str]) -> list[str]:
    out: list[str] = []
    for f in formats:
        key = resolve_format(f).key
        if key not in out:
            out.append(key)
    return out


def export_handoff(adapter: Any, spec: TimelineSpec | None, formats: Sequence[str], out_dir: Path,
                   project_path: Path | None) -> HandoffExport:
    """Export through the adapter; failures are recorded (not raised) so the rest of the package is still produced."""
    keys = normalize_formats(formats)
    res = HandoffExport(keys, dangerous_requested=[k for k in keys if k in DANGEROUS_FORMATS])
    try:
        res.report = adapter.export_interchange(keys, out_dir, spec=spec, project_path=project_path)
    except (FilmCraftError, OSError, ValueError) as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    otio = next((f for f in res.report.formats if f.format == "otio" and f.exported), None)
    if otio is not None:
        res.otio_reload = reload_otio(Path(otio.path), spec)
    return res


def reload_otio(path: Path, spec: TimelineSpec | None) -> dict[str, Any]:
    """Optional extra check with the OpenTimelineIO library (extra `otio`); reports 'skipped' when not installed."""
    try:
        import opentimelineio as otio  # type: ignore[import-not-found]
    except ImportError:
        return {"status": "skipped", "detail": "opentimelineio is not installed (pip install 'trip-vlog[otio]')"}
    try:
        tl = otio.adapters.read_from_file(str(path))
        frames = int(round(tl.duration().to_frames()))
        missing = [c.name for c in tl.find_clips() if c.media_reference is not None and
                   getattr(c.media_reference, "target_url", None) and not _url_exists(c.media_reference.target_url)]
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "detail": f"OpenTimelineIO could not read the file: {e}"}
    detail = {"status": "pass", "frames": frames, "clips": len(tl.find_clips()), "missing_media": missing}
    if spec is not None and abs(frames - spec.end_frame) > 1:
        detail.update(status="warn", detail=f"timeline is {frames} frames, the FilmCraft sequence is {spec.end_frame}")
    if missing:
        detail["status"] = "warn"
    return detail


def _url_exists(url: str) -> bool:
    from trip_vlog.filmcraft.interchange import url_to_path

    try:
        return Path(url_to_path(url)).exists()
    except (OSError, ValueError):
        return False
