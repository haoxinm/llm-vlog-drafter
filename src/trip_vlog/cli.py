"""Command line interface: index / status / resume / draft (stub until Phase 2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from trip_vlog import __version__
from trip_vlog.config import ConfigError
from trip_vlog.orchestrator import IndexResult, read_status, run_index


def _common(p: argparse.ArgumentParser, *, media_required: bool) -> None:
    p.add_argument("--media", type=Path, required=media_required, help="folder with the original photos/videos (read-only)")
    p.add_argument("--output", type=Path, required=True, help="output workspace folder (must be outside --media)")
    p.add_argument("--config", type=Path, help="project.yaml (default: <output>/project.yaml if present, else defaults)")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE",
                   help="override any config field, e.g. --set media.fingerprint=sha256 (repeatable)")
    p.add_argument("--fingerprint", choices=["fast", "sha256"], help="content fingerprint mode (media.fingerprint)")
    p.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None, help="scan sub-folders (media.recursive)")
    p.add_argument("--follow-symlinks", action=argparse.BooleanOptionalAction, default=None, help="follow symlinks (media.follow_symlinks)")
    p.add_argument("--ffprobe", help="path to ffprobe (media.ffprobe_path)")
    p.add_argument("--ffmpeg", help="path to ffmpeg (media.ffmpeg_path)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trip-vlog", description="Draft a travel vlog from photos and videos.")
    ap.add_argument("--version", action="version", version=f"trip-vlog {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="inventory media: scan, probe, catalog, thumbnails")
    _common(p, media_required=True)

    p = sub.add_parser("status", help="show the last run's stage status")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("resume", help="continue from the last valid stage (cached work is reused)")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--media", type=Path, help="media folder (default: the one recorded in manifest.json)")
    p.add_argument("--config", type=Path)
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE")

    p = sub.add_parser("draft", help="(stub) run index; planning/FilmCraft stages arrive in Phase 2")
    _common(p, media_required=True)
    p.add_argument("--duration", type=float, help="target duration in seconds (project.target_duration_seconds)")
    p.add_argument("--aspect", help="aspect ratio, e.g. 16:9 (project.aspect_ratio)")
    p.add_argument("--style", help="style (project.style)")
    p.add_argument("--filmcraft-mode", choices=["mcp", "cli", "bridge"], help="filmcraft.mode")
    p.add_argument("--auto-approve", action="store_true", default=None, help="workflow.approval=auto_approve")
    return ap


def _parse_sets(sets: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--set expects SECTION.FIELD=VALUE, got {item!r}")
        try:
            out[key.strip()] = yaml.safe_load(value)
        except yaml.YAMLError:
            out[key.strip()] = value
    return out


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    ov: dict[str, Any] = {}
    mapping = {
        "fingerprint": "media.fingerprint", "recursive": "media.recursive", "follow_symlinks": "media.follow_symlinks",
        "ffprobe": "media.ffprobe_path", "ffmpeg": "media.ffmpeg_path", "duration": "project.target_duration_seconds",
        "aspect": "project.aspect_ratio", "style": "project.style", "filmcraft_mode": "filmcraft.mode",
    }
    for attr, key in mapping.items():
        if getattr(args, attr, None) is not None:
            ov[key] = getattr(args, attr)
    if getattr(args, "auto_approve", None):
        ov["workflow.approval"] = "auto_approve"
    ov.update(_parse_sets(getattr(args, "sets", [])))  # --set wins over convenience flags
    return ov


def _print_index_result(res: IndexResult) -> None:
    print(f"run {res.run_id} -> {res.workspace}")
    for name, status in res.stage_status.items():
        print(f"  {name:<11} {status}")
    s = res.summary
    print(f"assets: {s['assets']}  probe failures: {s['probe_failed']}  stale: {s['stale']}")
    if s["probe_failed"]:
        print("  some files could not be probed; see logs/errors.jsonl and media_index/inventory_report.json")


def _cmd_index(args: argparse.Namespace, command: str = "index") -> int:
    res = run_index(args.media, args.output, args.config, _overrides(args), command=command)
    _print_index_result(res)
    return res.exit_code


def _cmd_resume(args: argparse.Namespace) -> int:
    res = run_index(args.media, args.output, args.config, _overrides(args), command="resume")
    _print_index_result(res)
    return res.exit_code


def _cmd_status(args: argparse.Namespace) -> int:
    info = read_status(args.output)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    m = info["manifest"]
    print(f"workspace : {info['workspace']}")
    print(f"tool      : trip-vlog {m.get('tool_version')}  platform: {m.get('platform', {}).get('system')} {m.get('platform', {}).get('machine')}")
    print(f"ffprobe   : {m.get('ffprobe', {}).get('version') or 'not found'}")
    print(f"last run  : {m.get('run_id')} ({m.get('command')}) started {m.get('run_started_at')} finished {m.get('run_finished_at') or '-'}")
    print("stages:")
    for name, st in m.get("stages", {}).items():
        extra = f" [{st['decision']}]" if st.get("decision") else ""
        print(f"  {name:<11} {st.get('status', 'pending'):<10}{extra} {json.dumps(st.get('summary', {}), sort_keys=True)}")
        if st.get("error"):
            print(f"      error: {st['error']}")
    cat = info.get("catalog")
    if cat:
        print(f"catalog   : {cat['assets_present']} present, {cat['assets_missing']} missing, schema v{cat['schema_version']}")
        for label, key in (("probe failures", "probe_failed"), ("stale (content changed)", "stale"),
                           ("thumbnail errors", "thumbnail_errors")):
            if cat[key]:
                print(f"  {label}: {len(cat[key])}")
                for p in cat[key][:10]:
                    print(f"    - {p}")
        for d in cat["probable_duplicates"]:
            print(f"  probable duplicates: {', '.join(d['paths'])}")
        for mv in cat["probable_moves"]:
            print(f"  probable move: {mv['from']} -> {mv['to']}")
    unfinished = [n for n, st in m.get("stages", {}).items() if st.get("status") not in ("succeeded",)]
    if unfinished:
        print(f"next: `trip-vlog resume --output {info['workspace']}` (unfinished: {', '.join(unfinished)})")
    return 0


def _cmd_draft(args: argparse.Namespace) -> int:
    code = _cmd_index(args, command="draft")
    print("draft: media inventory finished. Segmentation, planning and FilmCraft assembly land in Phase 2; nothing else was run.")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # robust output on legacy Windows code pages
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return {"index": _cmd_index, "status": _cmd_status, "resume": _cmd_resume, "draft": _cmd_draft}[args.command](args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted; run `trip-vlog resume` to continue", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
